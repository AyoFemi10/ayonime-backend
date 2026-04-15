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
    return {"stream_url": stream_url, "playlist_url": playlist_url}


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
