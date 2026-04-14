# ayonime-backend

FastAPI backend for AYONIME — handles AnimePahe streaming, episode fetching, and MP4 downloads.

## Stack
- Python 3.11
- FastAPI + Uvicorn
- AnimePahe API wrapper
- FFmpeg for video compilation

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install fastapi "uvicorn[standard]"
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

## Endpoints
- `GET /api/search?q=` — search anime
- `GET /api/airing` — currently airing
- `GET /api/anime/{slug}/episodes` — episode list
- `GET /api/stream` — get stream URL
- `POST /api/download` — start download job
- `GET /api/download/{job_id}/status` — poll progress
- `GET /api/download/{job_id}/file` — serve mp4
- `GET /health` — health check
