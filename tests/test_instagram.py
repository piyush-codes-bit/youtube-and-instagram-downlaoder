"""
Offline self-test for the Instagram pipeline. No network, no account, no pytest:

    python tests/test_instagram.py

A local HTTP server impersonates the Instagram CDN, yt-dlp extraction is stubbed, and the
real download/zip/job code runs against it.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instagram  # noqa: E402
from downloader import JobManager  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


# ------------------------------------------------------------------ fixture CDN


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): ...


def serve(directory: Path) -> tuple[socketserver.TCPServer, str]:
    handler = lambda *a, **kw: _Quiet(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def fake_carousel(base: str) -> dict:
    return {
        "_type": "playlist",
        "id": "CAROUSEL1",
        "title": "Three slide post",
        "channel": "tester",
        "entries": [
            {"id": "111", "url": f"{base}/a.mp4", "ext": "mp4", "title": "slide 1", "height": 1080},
            {"id": "222", "url": f"{base}/b.jpg", "ext": "jpg", "title": "slide 2"},
            {"id": "333", "url": f"{base}/c.jpg", "ext": "jpg", "title": "slide 3"},
        ],
    }


def fake_single(base: str) -> dict:
    return {
        "id": "SINGLE1",
        "title": "Single reel",
        "channel": "tester",
        "ext": "mp4",
        "url": f"{base}/a.mp4",
        "formats": [{"url": f"{base}/a.mp4", "ext": "mp4", "height": 720, "tbr": 900}],
    }


# ----------------------------------------------------------------------- tests


def test_classify() -> None:
    print("classify")
    cases = {
        "https://www.instagram.com/p/CabcDEF123/": ("post", "CabcDEF123"),
        "https://instagram.com/reel/Cxyz987/?igsh=zz": ("reel", "Cxyz987"),
        "https://www.instagram.com/reels/Cxyz987/": ("reel", "Cxyz987"),
        "https://www.instagram.com/tv/Ctv000/": ("igtv", "Ctv000"),
    }
    for url, (kind, code) in cases.items():
        got = instagram.classify(url)
        check(f"{kind} {code}", got["kind"] == kind and got["shortcode"] == code, str(got))
    story = instagram.classify("https://www.instagram.com/stories/nasa/3141592653/")
    check("story", story["kind"] == "story" and story["user"] == "nasa", str(story))
    hl = instagram.classify("https://www.instagram.com/stories/highlights/17900000000000000/")
    check("highlight", hl["kind"] == "highlight", str(hl))
    prof = instagram.classify("https://www.instagram.com/nasa/")
    check("profile", prof["kind"] == "profile" and prof["user"] == "nasa", str(prof))
    check("reject foreign host", not instagram.is_instagram("https://youtube.com/p/x"))
    check("accept short host", instagram.is_instagram("https://instagr.am/p/x/"))


def test_probe(base: str) -> None:
    print("probe")
    instagram._extract = lambda url, flat=False, limit=None: (  # type: ignore[assignment]
        fake_carousel(base) if "/p/" in url else fake_single(base)
    )
    car = instagram.probe("https://www.instagram.com/p/CAROUSEL1/")
    check("carousel kind", car["kind"] == "carousel", car["kind"])
    check("carousel items", car["item_count"] == 3, str(car["item_count"]))
    check("video typed", car["items"][0]["type"] == "video", str(car["items"][0]))
    check("image typed", car["items"][1]["type"] == "image", str(car["items"][1]))
    check("image thumb falls back to url", car["items"][1]["thumbnail"].endswith("b.jpg"))
    one = instagram.probe("https://www.instagram.com/reel/SINGLE1/")
    check("single kind", one["kind"] == "single" and one["item_count"] == 1, str(one["kind"]))
    check("platform tag", one["platform"] == "instagram")


def test_download(base: str, tmp: Path) -> None:
    print("run_job")
    seen: list[float] = []
    path = instagram.run_job(
        "https://www.instagram.com/p/CAROUSEL1/",
        tmp / "all",
        None,
        progress=lambda p, d, t: seen.append(p),
        cancelled=lambda: False,
    )
    check("zip produced", path.suffix == ".zip", path.name)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        sizes = [zf.getinfo(n).file_size for n in names]
    check("zip holds every item", len(names) == 3, str(names))
    check("zip bytes real", all(s > 0 for s in sizes), str(sizes))
    check("progress reached near 100", seen and max(seen) > 90, str(seen[-3:] if seen else []))
    check("progress monotonic", all(b >= a - 0.01 for a, b in zip(seen, seen[1:])))

    single = instagram.run_job(
        "https://www.instagram.com/reel/SINGLE1/",
        tmp / "one",
        None,
        progress=lambda p, d, t: None,
        cancelled=lambda: False,
    )
    check("single file, no zip", single.suffix == ".mp4", single.name)

    picked = instagram.run_job(
        "https://www.instagram.com/p/CAROUSEL1/",
        tmp / "picked",
        [2],
        progress=lambda p, d, t: None,
        cancelled=lambda: False,
    )
    check("selection honoured", picked.name.endswith(".jpg") and picked.name.startswith("03_"), picked.name)


def test_cancel(base: str, tmp: Path) -> None:
    print("cancel")
    try:
        instagram.run_job(
            "https://www.instagram.com/p/CAROUSEL1/",
            tmp / "cancel",
            None,
            progress=lambda p, d, t: None,
            cancelled=lambda: True,
        )
    except Exception as exc:
        check("cancel aborts job", type(exc).__name__ == "DownloadCancelled", type(exc).__name__)
    else:
        check("cancel aborts job", False, "completed anyway")


def test_manager(base: str, tmp: Path) -> None:
    print("job manager")
    mgr = JobManager(tmp / "downloads", max_workers=2, ttl_seconds=60)
    job = mgr.submit("https://www.instagram.com/p/CAROUSEL1/", mode="instagram", quality="best", engine="ig")
    for _ in range(80):
        time.sleep(0.1)
        snap = job.snapshot()
        if snap["status"] in ("done", "error"):
            break
    check("job done", snap["status"] == "done", snap.get("error") or snap["status"])
    check("job exposed file", bool(snap["download_url"]), str(snap["download_url"]))
    check("file on disk", job.path and Path(job.path).is_file())
    check("zip named", snap["filename"].endswith(".zip"), str(snap["filename"]))
    mgr.shutdown()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cdn = tmp / "cdn"
        cdn.mkdir()
        (cdn / "a.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(300_000))
        (cdn / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(120_000))
        (cdn / "c.jpg").write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(90_000))
        httpd, base = serve(cdn)
        try:
            test_classify()
            test_probe(base)
            test_download(base, tmp)
            test_cancel(base, tmp)
            test_manager(base, tmp)
        finally:
            httpd.shutdown()

    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())