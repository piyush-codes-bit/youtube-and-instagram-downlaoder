"""
Instagram pipeline.

Extraction is yt-dlp's job (cookies optional but recommended). Media bytes are pulled
straight off the signed CDN URLs it returns — Instagram serves progressive mp4/jpg, so
there is nothing to mux, and a plain HTTP read gives exact byte-level progress.

URL kinds: post (/p/), reel (/reel/, /reels/), igtv (/tv/), story (/stories/u/id/),
highlight (/stories/highlights/id/), profile (/user/), tag (/explore/tags/x/).
"""

from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from downloader import clean_error, cookies_opts

_HOST_RE = re.compile(r"^(?:www\.|m\.)?(?:instagram\.com|instagr\.am)$", re.IGNORECASE)
_PATH_RE = re.compile(
    r"^/(p|reel|reels|tv|stories|explore/tags)/([^/?#]+)(?:/([^/?#]+))?", re.IGNORECASE
)
_MEDIA_EXTS = {"mp4", "jpg", "jpeg", "webp", "png"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CHUNK = 256 * 1024

ProgressFn = Callable[[float, int, int], None]


class _Logger:
    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...


# ----------------------------------------------------------------- URL parsing


def is_instagram(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(_HOST_RE.match(host))


def classify(url: str) -> dict[str, Any]:
    """{kind, shortcode, user, url} — kind is post | reel | igtv | story | highlight | profile | tag."""
    parts = urlparse(url)
    m = _PATH_RE.match(parts.path or "/")
    if not m:
        handle = (parts.path or "/").strip("/").split("/")[0]
        if handle and not parts.path.startswith(("/p/", "/reel", "/tv")):
            return {"kind": "profile", "shortcode": None, "user": unquote(handle), "url": url}
        raise ValueError("Not an Instagram post, reel, story, or profile URL")

    section, second, third = m.group(1).lower(), unquote(m.group(2)), m.group(3)
    if section in ("p", "reel", "reels", "tv"):
        return {
            "kind": {"p": "post", "tv": "igtv"}.get(section, "reel"),
            "shortcode": second,
            "user": None,
            "url": url,
        }
    if section == "stories":
        if second.lower() == "highlights":
            return {"kind": "highlight", "shortcode": third, "user": None, "url": url}
        return {"kind": "story", "shortcode": third, "user": second, "url": url}
    return {"kind": "tag", "shortcode": second, "user": None, "url": url}


# ------------------------------------------------------------------- extraction


def _extract(url: str, *, flat: bool = False, limit: int | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(),
        "socket_timeout": 30,
        "retries": 3,
        "skip_download": True,
    }
    opts.update(cookies_opts())
    if flat:
        opts["extract_flat"] = "in_playlist"
    if limit:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("Instagram returned no data for that URL")
    return info


def _friendly_error(exc: BaseException) -> str:
    raw = clean_error(exc)
    low = raw.lower()
    needs_auth = (
        "login",
        "rate-limit",
        "rate limit",
        "429",
        "empty media response",
        "unable to extract data",
        "requires authentication",
        "checkpoint",
        "consent",
    )
    if "private" in low:
        return "That account is private — cookies from an account that follows it are required."
    if any(token in low for token in needs_auth):
        return (
            "Instagram refuses anonymous session data for this URL. Export cookies from a "
            "logged-in browser to cookies.txt (or set COOKIES_FROM_BROWSER=chrome) and retry. "
            f"Underlying error: {raw}"
        )
    return raw


def _thumbnail(entry: dict[str, Any]) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return thumbs[-1].get("url")
    if (entry.get("ext") or "") in {"jpg", "jpeg", "webp", "png"}:
        return entry.get("url")
    return None


def _media_type(entry: dict[str, Any]) -> str:
    ext = (entry.get("ext") or "").lower()
    if ext in {"mp4", "webm", "m4v"}:
        return "video"
    if ext in {"jpg", "jpeg", "webp", "png"}:
        return "image"
    if entry.get("vcodec") not in (None, "none"):
        return "video"
    return "image"


def _item(entry: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "id": entry.get("id"),
        "type": _media_type(entry),
        "url": entry.get("webpage_url") or entry.get("url"),
        "thumbnail": _thumbnail(entry),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "duration": entry.get("duration"),
        "title": entry.get("title") or entry.get("description"),
    }


def probe(url: str, *, limit: int = 24) -> dict[str, Any]:
    """Normalized metadata. kind: single | carousel | listing (profile/tag) | story."""
    if not is_instagram(url):
        raise ValueError("Not an Instagram URL")
    meta = classify(url)

    try:
        if meta["kind"] in ("profile", "tag"):
            info = _extract(url, flat=True, limit=limit)
            entries = [e for e in (info.get("entries") or []) if e]
            items = [
                {
                    "index": i,
                    "id": e.get("id"),
                    "type": "post",
                    "url": e.get("url") or e.get("webpage_url"),
                    "thumbnail": _thumbnail(e),
                    "width": None,
                    "height": None,
                    "duration": None,
                    "title": e.get("title") or e.get("id"),
                }
                for i, e in enumerate(entries)
            ]
            return {
                "platform": "instagram",
                "kind": "listing",
                "title": info.get("title") or meta["user"] or meta["shortcode"],
                "author": info.get("channel") or info.get("uploader") or meta["user"],
                "thumbnail": items[0]["thumbnail"] if items else None,
                "items": items,
                "item_count": len(items),
                "shortcode": meta["shortcode"],
                "note": "Profile listing: tick the posts you want, everything comes back as one zip.",
            }

        info = _extract(url)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            items = [_item(e, i) for i, e in enumerate(entries)]
            kind = "carousel"
        else:
            items = [_item(info, 0)]
            kind = "story" if meta["kind"] in ("story", "highlight") else "single"

        return {
            "platform": "instagram",
            "kind": kind,
            "title": info.get("title") or info.get("description") or meta["shortcode"] or "Instagram media",
            "author": info.get("channel") or info.get("uploader") or meta["user"],
            "thumbnail": _thumbnail(info) or (items[0]["thumbnail"] if items else None),
            "items": items,
            "item_count": len(items),
            "shortcode": meta["shortcode"],
            "note": None,
        }
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc


# ------------------------------------------------------------------- download


def _best_url(info: dict[str, Any]) -> tuple[str, str]:
    if info.get("url"):
        return info["url"], (info.get("ext") or "mp4").lower()
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    if not formats:
        raise RuntimeError("No downloadable stream on that item")
    best = max(
        formats,
        key=lambda f: (f.get("height") or 0, f.get("tbr") or 0, f.get("filesize") or 0),
    )
    return best["url"], (best.get("ext") or "mp4").lower()


class LinkExpired(RuntimeError):
    """Signed CDN URL aged out; re-extract the source and retry once."""


def _head_size(url: str, timeout: int = 8) -> int | None:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    try:
        req = Request(
            url,
            method="HEAD",
            headers={"User-Agent": _UA, "Referer": "https://www.instagram.com/"},
        )
        with urlopen(req, timeout=timeout) as resp:
            size = resp.headers.get("Content-Length")
            return int(size) if size and size.isdigit() else None
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None


def _fetch(
    media_url: str,
    dest_dir: Path,
    stem: str,
    *,
    on_bytes: Callable[[int], None],
    cancelled: Callable[[], bool],
) -> Path:
    """Plain HTTP read of a signed CDN URL, chunked, reporting bytes for this file."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    last: Exception | None = None
    for attempt in range(2):
        if cancelled():
            raise DownloadCancelled("cancelled")
        req = Request(media_url, headers={"User-Agent": _UA, "Referer": "https://www.instagram.com/"})
        try:
            with urlopen(req, timeout=60) as resp:
                ext = Path(urlparse(media_url).path).suffix.lower().lstrip(".") or "mp4"
                target = dest_dir / f"{stem}.{ext}"
                partial = target.with_suffix(target.suffix + ".part")
                got = 0
                with partial.open("wb") as fh:
                    while True:
                        if cancelled():
                            partial.unlink(missing_ok=True)
                            raise DownloadCancelled("cancelled")
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        on_bytes(got)
                partial.replace(target)
                return target
        except HTTPError as exc:
            last = exc
            if exc.code in (403, 410):
                raise LinkExpired(f"CDN link expired ({exc.code})") from exc
            if exc.code in (429, 500, 502, 503):
                time.sleep(1.0 + attempt)
                continue
            raise
        except (URLError, TimeoutError) as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"CDN fetch failed: {last}")


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") == "playlist":
        return [e for e in (info.get("entries") or []) if e]
    return [info]


def _refresh(source_url: str, entry_id: str | None) -> dict[str, Any] | None:
    """Re-extract a post and hand back the entry with the same id (fresh signed URL)."""
    try:
        for entry in _entries(_extract(source_url)):
            if str(entry.get("id")) == str(entry_id):
                return entry
    except Exception:
        return None
    return None


def run_job(
    url: str,
    workdir: Path,
    selection: list[int] | None,
    *,
    progress: ProgressFn,
    cancelled: Callable[[], bool],
) -> Path:
    """
    Download the selected items into workdir and return the deliverable: the single media
    file, or a zip when more than one item was produced.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    meta = classify(url)
    pairs: list[tuple[str, dict[str, Any], str]] = []  # (source page, media entry, filename label)

    if meta["kind"] in ("profile", "tag"):
        listing = _extract(url, flat=True, limit=48)
        posts = [e for e in (listing.get("entries") or []) if e]
        if not posts:
            raise RuntimeError("No posts found for that profile")
        chosen = [(i, posts[i]) for i in selection if i < len(posts)] if selection else list(enumerate(posts))
        for post_ord, (post_idx, post) in enumerate(chosen):
            post_url = post.get("webpage_url") or post.get("url")
            try:
                for media_ord, entry in enumerate(_entries(_extract(post_url))):
                    pairs.append((post_url, entry, f"{post_idx + 1:02d}-{media_ord + 1:02d}"))
            except Exception:
                continue  # one dead post must not kill the batch
        if not pairs:
            raise RuntimeError("Every selected post failed to extract (cookies needed?)")
    else:
        source = url
        entries = _entries(_extract(url))
        indexed = (
            [(i, entries[i]) for i in selection if i < len(entries)] if selection else list(enumerate(entries))
        )
        if not indexed:
            raise RuntimeError("Nothing selected to download")
        pairs = [(source, entry, f"{idx + 1:02d}") for idx, entry in indexed]

    sizes = [_head_size(_best_url(entry)[0]) for _, entry, _ in pairs]
    known = all(s for s in sizes)
    grand_total = sum(s for s in sizes if s) if known else 0
    total_units = len(pairs)
    produced: list[Path] = []
    done_bytes = 0

    for i, (source, entry, label) in enumerate(pairs):
        if cancelled():
            raise DownloadCancelled("cancelled")
        media_url, _ext = _best_url(entry)
        entry_id = re.sub(r"[^\w.-]+", "_", str(entry.get("id") or f"item{i}"))[:40]
        base = done_bytes

        def on_bytes(got: int, base: int = base, i: int = i) -> None:
            if known and grand_total:
                progress((base + got) / grand_total * 100.0, base + got, grand_total)
            else:
                progress(i / total_units * 100.0, base + got, 0)

        try:
            path = _fetch(media_url, workdir, f"{label}_{entry_id}", on_bytes=on_bytes, cancelled=cancelled)
        except LinkExpired:
            fresh = _refresh(source, entry.get("id"))
            if fresh is None:
                raise RuntimeError("Instagram CDN link expired and could not be refreshed — retry the job")
            path = _fetch(
                _best_url(fresh)[0], workdir, f"{label}_{entry_id}", on_bytes=on_bytes, cancelled=cancelled
            )
        done_bytes += path.stat().st_size
        produced.append(path)

    if not produced:
        raise RuntimeError("No media was downloaded")

    if len(produced) == 1:
        return produced[0]

    zip_path = workdir / re.sub(
        r"[^\w.-]+",
        "_",
        f"instagram_{meta.get('shortcode') or meta.get('user') or 'media'}_{len(produced)}items.zip",
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for p in produced:
            zf.write(p, arcname=p.name)
    for p in produced:
        p.unlink(missing_ok=True)
    return zip_path
