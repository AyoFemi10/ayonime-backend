"""
FastAPI backend that wraps AnimePaheAPI for the web frontend.
Supports streaming, download job management, and progress tracking.
"""

import sys
import os
import uuid
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Dict
from pathlib import Path

from anime_downloader.api.client import AnimePaheAPI
from anime_downloader.api.downloader import Downloader

app = FastAPI(title="AYONIME API", version="2.0.0")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

api = AnimePaheAPI()
downloader = Downloader(api)

# File-based job store so all 4 workers share state
import json
from pathlib import Path

JOBS_FILE = Path("/tmp/ayonime_jobs.json")
_jobs_lock = threading.Lock()

def _load_jobs() -> Dict[str, dict]:
    try:
        if JOBS_FILE.exists():
            return json.loads(JOBS_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_jobs(jobs: Dict[str, dict]):
    try:
        JOBS_FILE.write_text(json.dumps(jobs))
    except Exception:
        pass

def _get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _load_jobs().get(job_id)

def _set_job(job_id: str, data: dict):
    with _jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = data
        _save_jobs(jobs)
DOWNLOAD_DIR = Path.home() / "Downloads" / "ayonime"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── Search & Browse ──────────────────────────────────────────────────────────

@app.get("/api/search")
def search(q: str = Query(..., min_length=1)):
    results = api.search(q)
    return {"data": results}


@app.get("/api/airing")
def get_airing():
    data = api.check_for_updates()
    return {"data": data}


@app.get("/api/anime/{slug}/episodes")
def get_episodes(slug: str, anime_name: str = Query(default="")):
    episodes = api.fetch_episode_data(anime_name or slug, slug)
    if not episodes:
        raise HTTPException(status_code=404, detail="No episodes found")
    return {"data": episodes}


# ── Stream ───────────────────────────────────────────────────────────────────

@app.get("/api/stream")
def get_stream(
    anime_slug: str = Query(...),
    episode_session: str = Query(...),
    quality: str = Query(default="best"),
    audio: str = Query(default="jpn"),
):
    stream_url = api.get_stream_url(anime_slug, episode_session, quality, audio)
    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream not found")
    playlist_url = api.get_playlist_url(stream_url)
    if not playlist_url:
        raise HTTPException(status_code=404, detail="Playlist not found")

    # Fetch and parse the real playlist to get key + segments
    resp = api._request(playlist_url)
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch playlist")

    content = resp.data.decode("utf-8")
    base_url = playlist_url.rsplit("/", 1)[0]

    # Parse key URL and segments
    import re as _re
    key_url = None
    segments = []
    media_sequence = 0

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            try:
                media_sequence = int(line.split(":")[1])
            except Exception:
                pass
        elif line.startswith("#EXT-X-KEY"):
            m = _re.search('URI="([^"]+)"', line)
            if m:
                key_url = m.group(1)
        elif line and not line.startswith("#"):
            seg_url = line if line.startswith("http") else f"{base_url}/{line}"
            segments.append(seg_url)

    if not key_url or not segments:
        raise HTTPException(status_code=502, detail="Could not parse playlist")

    # Return a proxied m3u8 that points segments + key through our backend
    # The key endpoint will serve the real key, segment endpoint decrypts on the fly
    import urllib.parse as _up
    proxied_key = f"/api/proxy/key?url={_up.quote(key_url, safe='')}"
    proxied_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
        f'#EXT-X-KEY:METHOD=AES-128,URI="{proxied_key}"',
        "#EXT-X-TARGETDURATION:10",
    ]
    for i, seg_url in enumerate(segments):
        proxied_lines.append("#EXTINF:10.0,")
        idx = media_sequence + i
        proxied_lines.append(
            f"/api/proxy/segment?url={_up.quote(seg_url, safe='')}&key_url={_up.quote(key_url, safe='')}&idx={idx}"
        )
    proxied_lines.append("#EXT-X-ENDLIST")

    from fastapi.responses import Response as FastResponse
    proxied_m3u8 = "\n".join(proxied_lines)
    return {"stream_url": stream_url, "playlist_url": f"/api/proxy/playlist?url={_up.quote(playlist_url, safe='')}&key_url={_up.quote(key_url, safe='')}&base={_up.quote(base_url, safe='')}&seq={media_sequence}"}


@app.get("/api/proxy/playlist")
def proxy_playlist(
    url: str = Query(...),
    key_url: str = Query(...),
    base: str = Query(...),
    seq: int = Query(default=0),
):
    """Serve a rewritten m3u8 with proxied key + decrypted segment URLs."""
    import urllib.parse as _up
    import re as _re
    from fastapi.responses import Response as FastResponse

    resp = api._request(url)
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch playlist")

    content = resp.data.decode("utf-8")
    lines_out = []
    seg_index = seq

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY"):
            # Replace key URI with our proxy
            proxied_key = f"/api/proxy/key?url={_up.quote(key_url, safe='')}"
            lines_out.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{proxied_key}"')
        elif stripped and not stripped.startswith("#"):
            seg_url = stripped if stripped.startswith("http") else f"{base}/{stripped}"
            lines_out.append(
                f"/api/proxy/segment?url={_up.quote(seg_url, safe='')}&key_url={_up.quote(key_url, safe='')}&idx={seg_index}"
            )
            seg_index += 1
        else:
            lines_out.append(line)

    return FastResponse(
        content="\n".join(lines_out),
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


@app.get("/api/proxy/key")
def proxy_key(url: str = Query(...)):
    """Proxy the AES-128 decryption key with correct headers."""
    from fastapi.responses import Response as FastResponse
    resp = api._request(url)
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch key")
    return FastResponse(
        content=resp.data,
        media_type="application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/proxy/segment")
def proxy_segment(
    url: str = Query(...),
    key_url: str = Query(...),
    idx: int = Query(...),
):
    """Fetch, decrypt and serve a single .ts segment."""
    from fastapi.responses import Response as FastResponse
    from Crypto.Cipher import AES as _AES

    # Fetch key
    key_resp = api._request(key_url)
    if not key_resp:
        raise HTTPException(status_code=502, detail="Failed to fetch key")
    key = key_resp.data

    # Fetch encrypted segment
    seg_resp = api._request(url)
    if not seg_resp:
        raise HTTPException(status_code=502, detail="Failed to fetch segment")

    encrypted = seg_resp.data
    # Pad to AES block size
    while len(encrypted) % 16 != 0:
        encrypted += b"\0"

    # IV = segment index as 16-byte big-endian
    iv = idx.to_bytes(16, byteorder="big")
    cipher = _AES.new(key, _AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted)

    return FastResponse(
        content=decrypted,
        media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── Download ─────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    anime_slug: str
    episode_session: str
    anime_title: str
    episode_number: int
    quality: str = "best"
    audio: str = "jpn"


def _run_download(job_id: str, req: DownloadRequest):
    """Background thread: resolve stream → download segments → compile mp4."""
    job = _get_job(job_id)
    try:
        job["status"] = "resolving"
        _set_job(job_id, job)

        stream_url = api.get_stream_url(req.anime_slug, req.episode_session, req.quality, req.audio)
        if not stream_url:
            job["status"] = "failed"
            job["error"] = "Could not resolve stream URL"
            _set_job(job_id, job)
            return

        playlist_url = api.get_playlist_url(stream_url)
        if not playlist_url:
            job["status"] = "failed"
            job["error"] = "Could not resolve playlist URL"
            _set_job(job_id, job)
            return

        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in req.anime_title)
        ep_dir = DOWNLOAD_DIR / safe_title / f"ep{req.episode_number}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        output_mp4 = DOWNLOAD_DIR / safe_title / f"{safe_title}_ep{req.episode_number}.mp4"

        if output_mp4.exists():
            job["status"] = "done"
            job["progress"] = 100
            job["file_path"] = str(output_mp4)
            _set_job(job_id, job)
            return

        job["status"] = "downloading"
        _set_job(job_id, job)

        playlist_path = downloader.fetch_playlist(playlist_url, str(ep_dir))
        if not playlist_path:
            job["status"] = "failed"
            job["error"] = "Failed to fetch playlist"
            _set_job(job_id, job)
            return

        ok = downloader.download_from_playlist_cli(playlist_path, num_threads=8)
        if not ok:
            job["status"] = "failed"
            job["error"] = "Segment download failed"
            _set_job(job_id, job)
            return

        job["status"] = "compiling"
        _set_job(job_id, job)

        def on_progress(pct: int):
            j = _get_job(job_id)
            j["progress"] = pct
            _set_job(job_id, j)

        compiled = downloader.compile_video(str(ep_dir), str(output_mp4), on_progress)
        if not compiled:
            job["status"] = "failed"
            job["error"] = "FFmpeg compilation failed"
            _set_job(job_id, job)
            return

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = str(output_mp4)
        _set_job(job_id, job)

    except Exception as e:
        job = _get_job(job_id) or {}
        job["status"] = "failed"
        job["error"] = str(e)
        _set_job(job_id, job)


@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "file_path": None,
        "error": None,
        "anime_title": req.anime_title,
        "episode_number": req.episode_number,
    }
    _set_job(job_id, job)
    t = threading.Thread(target=_run_download, args=(job_id, req), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/download/{job_id}/status")
def download_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/download/{job_id}/file")
def download_file(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done" or not job["file_path"]:
        raise HTTPException(status_code=400, detail="File not ready")
    path = Path(job["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(path=str(path), media_type="video/mp4", filename=path.name)


@app.get("/api/downloads")
def list_downloads():
    return {"jobs": list(_load_jobs().values())}


@app.get("/health")
def health():
    return {"status": "ok"}
