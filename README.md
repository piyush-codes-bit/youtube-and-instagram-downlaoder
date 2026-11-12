# ytdl-flask

Flask API + single-page UI for pulling YouTube and Instagram media through yt-dlp. Pick a
resolution, pick audio-only, pick the audio codec, or grab an Instagram reel/carousel/profile
feed as a zip. Downloads run in a worker pool with live progress, so the request returns
immediately instead of hanging on a 4K stream.

```
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Layout

```
app.py            Flask routes
downloader.py     yt-dlp engine, job pool, TTL janitor, cookie config
instagram.py      Instagram classify / probe / per-item CDN download / zip
templates/        index.html (vanilla JS, no build step)
tests/            offline fixture tests for the Instagram pipeline
downloads/        per-job work dirs, auto-purged
```

## Endpoints

| Method | Path | Body / Notes |
|---|---|---|
| GET | `/` | UI |
| GET | `/api/health` | yt-dlp version, ffmpeg availability, job counts |
| POST | `/api/info` | `{"url": "..."}` → title, channel, `heights[]`, `audio_formats[]` |
| POST | `/api/download` | `{"url","mode","quality","audio_format","selection"}` → `202 {job}` |
| GET | `/api/jobs` | last 50 jobs |
| GET | `/api/jobs/<id>` | poll one job: `status`, `phase`, `percent`, `speed`, `eta` |
| GET | `/api/jobs/<id>/file` | stream the finished file, `Content-Disposition: attachment` |
| DELETE | `/api/jobs/<id>` | cancel if running, remove file if finished |

`mode` is `video` or `audio`. `quality` is `best` or a pixel height (`2160`, `1440`, `1080`,
`720`, `480`, `360`, `240`). `audio_format` is one of `mp3`, `m4a`, `opus`, `wav`.
`selection` is a list of item indexes from `/api/info` — Instagram only, omit or `null` for
"everything".

```bash
curl -s localhost:5000/api/info -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"}'

curl -s localhost:5000/api/download -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw","mode":"video","quality":"1080"}'

curl -s localhost:5000/api/jobs/<id>
curl -OJ localhost:5000/api/jobs/<id>/file
```

## Job model

`queued → running (downloading → processing) → done | error | cancelled`

The API is asynchronous by design: yt-dlp blocks for minutes on long videos, so the POST
returns a job id and the UI polls `/api/jobs/<id>` every 800ms. Three workers by default;
extra jobs queue. Files live in `downloads/<job_id>/` and are deleted an hour after they
finish (janitor thread, 60s sweep). Cancel raises `DownloadCancelled` from the progress
hook and the work dir is removed.

## Instagram

Handled by `instagram.py`, routed automatically by host — the same `/api/info` and
`/api/download` endpoints serve both platforms.

| URL | Result |
|---|---|
| `/p/<code>/`, `/reel/<code>/`, `/reels/<code>/`, `/tv/<code>/` | post / reel / igtv, carousel-aware |
| `/stories/<user>/<id>/` | single story item |
| `/stories/highlights/<id>/` | highlight (cookies almost always required) |
| `/<user>/`, `/explore/tags/<tag>/` | listing of the last 24–48 items, each one downloadable |

- `/api/info` returns `items[]` with `type` (`image` \| `video` \| `post`), `thumbnail`, and
  `index`. The UI turns that into a thumbnail grid; tick what you want.
- One selected item → the media file. More than one → a single zip
  (`instagram_<code>_<n>items.zip`). Selection is re-validated server-side.
- Bytes come straight off the signed CDN URL with plain HTTP: Instagram already serves
  progressive mp4/jpg, so there is nothing to mux and ffmpeg is not involved. `HEAD` gives
  exact sizes for byte-accurate progress; a 403/410 means the signed link aged out, so the
  post is re-extracted once and the entry retried by id.

**Auth.** Instagram throttles anonymous clients hard — expect `rate-limit`, `empty media
response`, or `login required` without a session. Two ways to supply one:

```bash
# exported Netscape cookie file (browser extension, or yt-dlp --cookies-from-browser)
COOKIES_FILE=C:\path\to\cookies.txt python app.py

# or read them straight from an installed browser profile
set COOKIES_FROM_BROWSER=chrome
python app.py
```

`COOKIES_FROM_BROWSER` accepts `browser[:profile]` (e.g. `firefox:default-release`). Chrome's
cookie store must be readable — close the browser if it locks the DB. `/api/health` reports
which of the two (if any) is active under `cookies`.

Errors that mean "you need cookies" are rewritten into a readable message instead of dumping
yt-dlp's raw traceback: `empty media response`, `unable to extract data`, `429`, `login`,
`checkpoint`, `consent`.

## Format selection

- **Video, ffmpeg present:** `bv*[height<=H][vcodec^=avc1]+ba[ext=m4a]` first — that combo
  muxes to H.264/AAC mp4, which plays literally everywhere — then any codec, then a
  progressive stream as a last resort.
- **Video, no ffmpeg:** progressive `b[height<=H]` only. YouTube has dropped muxed streams
  on most uploads, so expect "format is not available" above 720p, or at all on newer videos.
- **Audio, ffmpeg present:** `bestaudio/best` + `FFmpegExtractAudio` → mp3/m4a at ~192k,
  opus at ~160k, wav lossless.
- **Audio, no ffmpeg:** native container only (m4a/opus), no conversion, no quality knob.

`imageio-ffmpeg` is in requirements so a fresh machine has a working ffmpeg with zero setup.
`find_ffmpeg()` order: `PATH` → `imageio_ffmpeg` → `./bin/ffmpeg[.exe]`.

## Environment

| Var | Default | |
|---|---|---|
| `HOST` | `127.0.0.1` | |
| `PORT` | `5000` | |
| `DOWNLOAD_DIR` | `./downloads` | |
| `MAX_WORKERS` | `3` | concurrent yt-dlp processes |
| `FILE_TTL_SECONDS` | `3600` | delete finished files after |
| `COOKIES_FILE` | `./cookies.txt` if present | Netscape cookie jar |
| `COOKIES_FROM_BROWSER` | unset | `chrome`, `firefox:profile`, … |

Production: `waitress-serve --port=5000 app:app` (the dev server is threaded but not hardened).

## Tests

```bash
python tests/test_instagram.py
```

Offline — a local HTTP server impersonates the Instagram CDN, extraction is stubbed, and the
real classify/probe/download/zip/cancel/job-manager code runs against it. No account, no
network, no pytest.

## Limits worth knowing

- Age-restricted, members-only, and DRM'd videos fail at extract time; the error surfaces
  verbatim in the job record.
- Instagram: private accounts and highlights need cookies; profiles list only the first page
  of posts; story URLs expire fast and can fail mid-job (retry the job).
- Playlist URLs collapse to the first entry (`noplaylist: True`).
- Live streams are reported in `/api/info` but queue as an unbounded download.
- Polling is intentional — swap in SSE in `poll()` if you want push instead.
- Downloading copyrighted material without permission is on you, not the code.