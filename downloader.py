"""
Download engine. yt-dlp wrapper + in-process job pool. No Flask imports here so the
module is testable on its own.

Job lifecycle: queued -> running (downloading -> processing) -> done | error | cancelled.
Every job owns its own subdirectory under DOWNLOAD_DIR so cleanup is a single rmtree.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadCancelled

VIDEO_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240)
AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav")

_URL_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".wav", ".ogg", ".flac", ".aac"}
_JUNK_SUFFIXES = (".part", ".ytdl", ".temp")
_MAX_STEM = 150


class _NullLogger:
    """Swallows yt-dlp's console output; the job record is the only channel we read."""

    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...


def find_ffmpeg() -> str | None:
    """PATH first, then a bundled imageio-ffmpeg binary, then a bin/ next to this file."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # optional dependency: pip install imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    local = Path(__file__).resolve().parent / "bin" / exe
    return str(local) if local.is_file() else None


FFMPEG = find_ffmpeg()


def _ffmpeg_location(path: str | None) -> str | None:
    """yt-dlp wants a directory for a standard ffmpeg binary, a full path for anything else
    (imageio-ffmpeg ships `ffmpeg-win-x86_64-vX.exe`, which no lookup would find)."""
    if not path:
        return None
    p = Path(path)
    return str(p.parent if p.name.lower() in ("ffmpeg", "ffmpeg.exe") else p)


_FFMPEG_LOCATION = _ffmpeg_location(FFMPEG)

_BASE_DIR = Path(__file__).resolve().parent
_COOKIE_FILE = os.environ.get("COOKIES_FILE") or (
    str(_BASE_DIR / "cookies.txt") if (_BASE_DIR / "cookies.txt").is_file() else None
)
_COOKIES_BROWSER = os.environ.get("COOKIES_FROM_BROWSER")  # e.g. "chrome" or "firefox:default"


def cookies_opts() -> dict[str, Any]:
    """Auth for extractors that throttle or hide media from anonymous clients."""
    opts: dict[str, Any] = {}
    if _COOKIE_FILE and Path(_COOKIE_FILE).is_file():
        opts["cookiefile"] = _COOKIE_FILE
    if _COOKIES_BROWSER:
        browser, _, profile = _COOKIES_BROWSER.partition(":")
        opts["cookiesfrombrowser"] = (browser.strip().lower(), profile.strip() or None)
    return opts


def has_cookies() -> bool:
    return bool(cookies_opts())


def cookie_summary() -> str:
    """Human-readable auth state, shown in /api/health."""
    if _COOKIE_FILE and Path(_COOKIE_FILE).is_file():
        return f"file:{Path(_COOKIE_FILE).name}"
    if _COOKIES_BROWSER:
        return f"browser:{_COOKIES_BROWSER}"
    return "none"


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")
    if len(url) > 2048:
        raise ValueError("URL is too long")
    if not _URL_SCHEME.match(url):
        raise ValueError("URL must start with http:// or https://")
    return url


def _clean_selection(selection: list[int] | None) -> list[int] | None:
    if not selection:
        return None
    if not isinstance(selection, list) or len(selection) > 200:
        raise ValueError("selection must be a list of at most 200 item indexes")
    out: list[int] = []
    for raw in selection:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            raise ValueError("selection indexes must be integers") from None
        if idx < 0 or idx > 9999:
            raise ValueError("selection index out of range")
        if idx not in out:
            out.append(idx)
    return out or None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    text = _ANSI_RE.sub("", text)
    for prefix in ("ERROR: ", "error: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text[:400]


def build_video_format(quality: str, have_ffmpeg: bool) -> str:
    """
    Prefer avc1+m4a so the output plays everywhere without re-encoding; the chain
    falls back to any codec, then to a progressive only stream when ffmpeg is absent.
    """
    q = (quality or "best").lower()
    if not have_ffmpeg:
        if q in ("best", "", "max"):
            return "b/bv*+ba"
        height = int(q) if q.isdigit() else 720
        return f"b[height<={height}]/b[height<={height}]/b"

    if q in ("best", "", "max"):
        return "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b"
    height = int(q) if q.isdigit() else 720
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*[height<={height}]+ba"
        f"/b[height<={height}]"
        f"/bv*+ba/b"
    )


def build_audio_format(have_ffmpeg: bool) -> str:
    return "bestaudio/best" if have_ffmpeg else "bestaudio[ext=m4a]/bestaudio/best"


@dataclass
class Job:
    url: str
    mode: str  # "video" | "audio"
    quality: str
    audio_format: str
    engine: str = "yt"  # "yt" | "ig"
    platform: str = "youtube"
    selection: list[int] | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "queued"  # queued | running | done | error | cancelled
    phase: str = "queued"  # queued | downloading | processing | finished
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: int | None = None
    title: str | None = None
    filename: str | None = None
    path: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    delivered: float | None = None
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def ready_since(self) -> float:
        return self.delivered or self.finished or self.created

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "engine": self.engine,
                "platform": self.platform,
                "mode": self.mode,
                "quality": self.quality,
                "audio_format": self.audio_format,
                "status": self.status,
                "phase": self.phase,
                "percent": round(self.percent, 1),
                "downloaded": self.downloaded,
                "total": self.total,
                "speed": self.speed,
                "eta": self.eta,
                "title": self.title,
                "filename": self.filename,
                "error": self.error,
                "created": self.created,
                "elapsed": round((self.finished or time.time()) - self.created, 1),
                "download_url": f"/api/jobs/{self.id}/file" if self.status == "done" else None,
            }


class JobManager:
    """Bounded worker pool + TTL janitor. Safe for Flask's threaded request handling."""

    def __init__(
        self,
        download_dir: str | Path,
        *,
        max_workers: int = 3,
        ttl_seconds: int = 3600,
        max_jobs: int = 200,
    ) -> None:
        self.root = Path(download_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self.max_jobs = max_jobs
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ytdl")
        self._janitor = threading.Thread(target=self._reap_loop, name="ytdl-janitor", daemon=True)
        self._stop = threading.Event()
        self._janitor.start()

    # ---------------------------------------------------------------- public

    def submit(
        self,
        url: str,
        mode: str,
        quality: str,
        audio_format: str = "mp3",
        *,
        engine: str = "yt",
        platform: str = "youtube",
        selection: list[int] | None = None,
    ) -> Job:
        url = validate_url(url)
        mode = (mode or "video").lower()
        if engine == "ig":
            platform = "instagram"
            mode = "instagram"
            quality = "best"
            audio_format = ""
            selection = _clean_selection(selection)
        else:
            if mode not in ("video", "audio"):
                raise ValueError("mode must be 'video' or 'audio'")
            if mode == "audio":
                audio_format = (audio_format or "mp3").lower()
                if audio_format not in AUDIO_FORMATS:
                    raise ValueError(f"audio_format must be one of {', '.join(AUDIO_FORMATS)}")
                quality = "audio"
            else:
                quality = (quality or "best").lower()
                if quality != "best" and not quality.isdigit():
                    raise ValueError("quality must be 'best' or a height in pixels")
                audio_format = ""

        job = Job(
            url=url,
            mode=mode,
            quality=quality,
            audio_format=audio_format,
            engine=engine,
            platform=platform,
            selection=selection,
        )
        with self._lock:
            self._evict_locked()
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created, reverse=True)
        return [j.snapshot() for j in jobs[:limit]]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "error", "cancelled"):
            return False
        job.cancelled = True
        if job.status == "queued":
            self._finish(job, status="cancelled", error="Cancelled by user")
        return True

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(self.root / job_id, ignore_errors=True)
        return True

    def stats(self) -> dict[str, Any]:
        with self._lock:
            jobs = list(self._jobs.values())
        active = sum(1 for j in jobs if j.status in ("queued", "running"))
        return {"jobs": len(jobs), "active": active, "ffmpeg": bool(FFMPEG)}

    def shutdown(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --------------------------------------------------------------- internals

    def _evict_locked(self) -> None:
        if len(self._jobs) < self.max_jobs:
            return
        oldest = sorted(self._jobs.values(), key=lambda j: j.created)[: max(1, self.max_jobs // 4)]
        for job in oldest:
            if job.status in ("done", "error", "cancelled"):
                self._jobs.pop(job.id, None)
                shutil.rmtree(self.root / job.id, ignore_errors=True)

    def _run(self, job: Job) -> None:
        workdir = self.root / job.id
        workdir.mkdir(parents=True, exist_ok=True)
        with job._lock:
            if job.cancelled:
                return
            job.status = "running"
            job.phase = "downloading"

        if job.engine == "ig":
            self._run_instagram(job, workdir)
            return

        want_audio = job.mode == "audio"
        use_ffmpeg = bool(FFMPEG)
        fmt = build_audio_format(use_ffmpeg) if want_audio else build_video_format(job.quality, use_ffmpeg)

        opts: dict[str, Any] = {
            "format": fmt,
            "outtmpl": str(workdir / f"%(title).{_MAX_STEM}B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _NullLogger(),
            "progress_hooks": [self._progress_hook(job)],
            "postprocessor_hooks": [self._postprocessor_hook(job)],
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "nocheckcertificate": False,
            "windowsfilenames": True,
            "overwrites": True,
            "paths": {"home": str(workdir), "temp": str(workdir)},
        }
        if _FFMPEG_LOCATION:
            opts["ffmpeg_location"] = _FFMPEG_LOCATION
        opts.update(cookies_opts())

        if want_audio and use_ffmpeg:
            codec = job.audio_format
            pp: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec in ("mp3", "m4a"):
                pp["preferredquality"] = "192"
            elif codec == "opus":
                pp["preferredquality"] = "160"
            opts["postprocessors"] = [pp]
        else:
            opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=True)
                if info is None:
                    raise RuntimeError("yt-dlp returned no media for that URL")
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise RuntimeError("Playlist is empty")
                    info = entries[0]

            if job.cancelled:
                raise DownloadCancelled("cancelled")

            produced = _pick_output(workdir, info)
            if produced is None:
                raise RuntimeError("Download finished but no media file was produced")

            with job._lock:
                job.title = info.get("title") or produced.stem
                job.path = str(produced)
                job.filename = produced.name
                job.total = produced.stat().st_size
                job.downloaded = job.total
                job.percent = 100.0
                job.speed = 0.0
                job.eta = 0
                job.phase = "finished"
            self._finish(job, status="done")

        except DownloadCancelled:
            self._finish(job, status="cancelled", error="Cancelled by user")
        except Exception as exc:  # yt-dlp raises DownloadError and friends
            self._finish(job, status="error", error=clean_error(exc))
        finally:
            if job.status in ("error", "cancelled"):
                shutil.rmtree(workdir, ignore_errors=True)

    def _run_instagram(self, job: Job, workdir: Path) -> None:
        import instagram  # deferred: instagram imports this module

        def report(pct: float, done: int, total: int) -> None:
            with job._lock:
                job.percent = max(0.0, min(99.5, pct))
                job.downloaded = done
                if total:
                    job.total = total
                job.phase = "downloading"

        try:
            path = instagram.run_job(
                job.url,
                workdir,
                job.selection,
                progress=report,
                cancelled=lambda: job.cancelled,
            )
            if job.cancelled:
                raise DownloadCancelled("cancelled")
            with job._lock:
                job.title = path.stem
                job.filename = path.name
                job.path = str(path)
                job.total = path.stat().st_size
                job.downloaded = job.total
                job.percent = 100.0
                job.phase = "finished"
            self._finish(job, status="done")
        except DownloadCancelled:
            self._finish(job, status="cancelled", error="Cancelled by user")
        except Exception as exc:
            self._finish(job, status="error", error=clean_error(exc))
        finally:
            if job.status in ("error", "cancelled"):
                shutil.rmtree(workdir, ignore_errors=True)

    def _finish(self, job: Job, *, status: str, error: str | None = None) -> None:
        with job._lock:
            job.status = status
            job.error = error or job.error
            job.finished = time.time()
            if status != "done":
                job.phase = status

    def _progress_hook(self, job: Job):
        def hook(d: dict[str, Any]) -> None:
            if job.cancelled:
                raise DownloadCancelled("cancelled")
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                with job._lock:
                    job.phase = "downloading"
                    job.downloaded = done
                    job.total = total
                    job.percent = (done / total * 100.0) if total else 0.0
                    job.speed = d.get("speed") or 0.0
                    job.eta = d.get("eta")
            elif status == "finished":
                with job._lock:
                    job.phase = "processing"
                    job.speed = 0.0
                    job.eta = 0
                    job.percent = 99.0

        return hook

    def _postprocessor_hook(self, job: Job):
        def hook(d: dict[str, Any]) -> None:
            if job.cancelled:
                raise DownloadCancelled("cancelled")
            with job._lock:
                job.phase = "processing" if d.get("status") != "finished" else "finished"
                if job.phase == "processing":
                    job.percent = max(job.percent, 99.0)

        return hook

    def _reap_loop(self) -> None:
        while not self._stop.wait(60):
            cutoff = time.time() - self.ttl
            with self._lock:
                stale = [j for j in self._jobs.values() if j.status in ("done", "error", "cancelled") and j.ready_since < cutoff]
                for job in stale:
                    self._jobs.pop(job.id, None)
            for job in stale:
                shutil.rmtree(self.root / job.id, ignore_errors=True)


def _pick_output(workdir: Path, info: dict[str, Any]) -> Path | None:
    for entry in info.get("requested_downloads") or []:
        fp = entry.get("filepath")
        if fp:
            candidate = Path(fp)
            if candidate.is_file() and candidate.suffix.lower() in _MEDIA_EXTS:
                return candidate
    files = [
        p
        for p in workdir.rglob("*")
        if p.is_file() and p.suffix.lower() in _MEDIA_EXTS and not p.name.endswith(_JUNK_SUFFIXES)
    ]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def probe(url: str, *, timeout: int = 30) -> dict[str, Any]:
    """Metadata + which qualities the video actually offers, so the UI never shows a dead option."""
    url = validate_url(url)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "logger": _NullLogger(),
        "socket_timeout": timeout,
        "extract_flat": "in_playlist",
    }
    if _FFMPEG_LOCATION:
        opts["ffmpeg_location"] = _FFMPEG_LOCATION

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("No metadata returned for that URL")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Playlist is empty")
        info = entries[0]

    heights = sorted(
        {
            f["height"]
            for f in (info.get("formats") or [])
            if f.get("height") and f.get("vcodec") and f["vcodec"] != "none"
        },
        reverse=True,
    )
    audio_only = [
        f
        for f in (info.get("formats") or [])
        if f.get("acodec") and f["acodec"] != "none" and (not f.get("vcodec") or f["vcodec"] == "none")
    ]
    audio_bitrates = sorted({f.get("abr") for f in audio_only if f.get("abr")}, reverse=True)

    return {
        "platform": "youtube",
        "kind": "single",
        "items": [],
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "is_live": bool(info.get("is_live")),
        "heights": [h for h in heights if h >= 144],
        "audio_bitrates": audio_bitrates[:4],
        "audio_formats": list(AUDIO_FORMATS) if FFMPEG else ["m4a"],
        "ffmpeg": bool(FFMPEG),
        "qualities": ["best"] + [str(h) for h in heights if h >= 144],
    }