"""
Flask API for downloading YouTube media with yt-dlp.

    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000

Routes
    GET  /                        UI
    GET  /api/health              tool versions + ffmpeg availability
    POST /api/info                {url} -> metadata + available qualities
    POST /api/download            {url, mode, quality, audio_format} -> {job_id}
    GET  /api/jobs                list jobs
    GET  /api/jobs/<id>           poll one job
    GET  /api/jobs/<id>/file      fetch the finished file
    DELETE /api/jobs/<id>         cancel + remove
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, render_template, request, send_file

import instagram
from downloader import FFMPEG, JobManager, clean_error, cookie_summary, probe

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))
TTL_SECONDS = int(os.environ.get("FILE_TTL_SECONDS", "3600"))

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

manager = JobManager(DOWNLOAD_DIR, max_workers=MAX_WORKERS, ttl_seconds=TTL_SECONDS)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "yt_dlp": yt_dlp.version.__version__,
            "ffmpeg": bool(FFMPEG),
            "ffmpeg_path": FFMPEG,
            "cookies": cookie_summary(),
            "download_dir": str(manager.root),
            "ttl_seconds": TTL_SECONDS,
            **manager.stats(),
        }
    )


@app.post("/api/info")
def info():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or request.form.get("url") or "").strip()
    try:
        if instagram.is_instagram(url):
            data = instagram.probe(url)
        else:
            data = probe(url)
        return jsonify(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": clean_error(exc)}), 400


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    try:
        if instagram.is_instagram(payload.get("url") or ""):
            job = manager.submit(
                url=payload.get("url"),
                mode="instagram",
                quality="best",
                engine="ig",
                selection=payload.get("selection"),
            )
        else:
            job = manager.submit(
                url=payload.get("url"),
                mode=payload.get("mode", "video"),
                quality=str(payload.get("quality", "best")),
                audio_format=payload.get("audio_format", "mp3"),
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job.snapshot()), 202


@app.get("/api/jobs")
def list_jobs():
    return jsonify({"jobs": manager.list_jobs(limit=50)})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job.snapshot())


@app.get("/api/jobs/<job_id>/file")
def get_file(job_id: str):
    job = manager.get(job_id)
    if job is None:
        abort(404, description="job not found")
    if job.status != "done" or not job.path:
        abort(409, description="job is not finished")
    path = Path(job.path)
    if not path.is_file():
        abort(410, description="file expired")
    job.delivered = time.time()
    return send_file(
        path,
        as_attachment=True,
        download_name=job.filename or path.name,
        conditional=True,
        max_age=0,
    )


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    if not manager.delete(job_id):
        return jsonify({"error": "job not found"}), 404
    return jsonify({"ok": True})


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal error"}), 500
    return "internal error", 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"  yt-dlp  {yt_dlp.version.__version__}")
    print(f"  ffmpeg  {FFMPEG or 'not found - progressive formats + raw audio only'}")
    print(f"  files   {manager.root}")
    app.run(host=host, port=port, threaded=True, debug=False)