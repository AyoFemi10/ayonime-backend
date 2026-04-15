"""
AYONIME FastAPI Backend
- Streaming: fully proxied through our domain, kwik never exposed to browser
- Downloading: background job with progress tracking
"""

import sys, os, uuid, threading, json, re
import urllib.parse as _up
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional, Dict
from pathlib import Path
from Crypto.Cipher import AES

from anime_downloader.api.client import AnimePaheAPI
from anime_downloader.api.downloader import Downloader

app = FastAPI(title="AYONIME API", version="3.0.0")

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

api = AnimePaheAPI()
downloader = Downloader(api)

DOWNLOAD_DIR = Path.home() / "Downloads" / "ayonime"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Shared file-based job store (survives across workers) ────────────────────
JOBS_FILE = Path("/tmp/ayonime_jobs.json")
_lock = threading.Lock()

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
    with _lock:
        return _load_jobs().get(job_id)

def _set_job(job_id: str, data: dict):
    with _lock:
        jobs = _load_jobs()
        jobs[job_id] = data
        _save_jobs(jobs)


# ── Browse / Search ──────────────────────────────────────────────────────────

@app.get("/api/search")
def search(q: str = Query(..., min_length=1)):
    return {"data": api.search(q)}

@app.get("/api/airing")
def get_airing():
    return {"data": api.check_for_updates()}

@app.get("/api/anime/{slug}/episodes")
def get_episodes(slug: str, anime_name: str = Query(default="")):
    episodes = api.fetch_episode_data(anime_name or slug, slug)
    if not episodes:
        raise HTTPException(status_code=404, detail="No episodes found")
    return {"data": episodes}


# ── Streaming ────────────────────────────────────────────────────────────────

@app.get("/api/stream")
def get_stream(
    anime_slug: str = Query(...),
    episode_session: str = Query(...),
    quality: str = Query(default="best"),
    audio: str = Query(default="jpn"),
):
    """
    Resolve stream → get m3u8 URL → return a proxied player URL.
    The real m3u8/kwik URL never reaches the browser.
    """
    kwik_url = api.get_stream_url(anime_slug, episode_session, quality, audio)
    if not kwik_url:
        raise HTTPException(status_code=404, detail="Stream not found")

    m3u8_url = api.get_playlist_url(kwik_url)
    if not m3u8_url:
        raise HTTPException(status_code=404, detail="Playlist not found")

    token = _up.quote(m3u8_url, safe="")
    return {"stream_url": f"/api/player?token={token}&_={uuid.uuid4().hex[:8]}", "playlist_url": None}


@app.get("/api/player")
def get_player(token: str = Query(...)):
    """
    Serve a self-contained HLS player page from our domain.
    Uses hls.js + our /api/proxy/* endpoints to decrypt and stream.
    Browser only ever talks to apis.ayohost.site — kwik is invisible.
    """
    m3u8_url = _up.unquote(token)
    encoded = _up.quote(m3u8_url, safe="")
    api_origin = os.environ.get("API_ORIGIN", "https://apis.ayohost.site")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#000;overflow:hidden}}
video{{width:100%;height:100%;object-fit:contain;display:block}}
</style>
</head>
<body>
<video id="v" controls autoplay playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
(function(){{
  var origin = "{api_origin}";
  var src = origin + "/api/proxy/m3u8?url={encoded}";
  var video = document.getElementById("v");

  if (typeof Hls !== "undefined" && Hls.isSupported()) {{
    var hls = new Hls({{
      enableWorker: false,
      lowLatencyMode: false,
      progressive: false,
      testBandwidth: false,
      abrEwmaDefaultEstimate: 500000,
    }});
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, function() {{
      video.play().catch(function(){{}});
    }});
    hls.on(Hls.Events.ERROR, function(e, data) {{
      if (data.fatal) {{
        console.error("HLS fatal error", data.type, data.details);
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {{
          setTimeout(function(){{ hls.startLoad(); }}, 1000);
        }} else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {{
          hls.recoverMediaError();
        }}
      }}
    }});
  }} else if (video.canPlayType("application/vnd.apple.mpegurl")) {{
    video.src = src;
    video.play().catch(function(){{}});
  }} else {{
    document.body.innerHTML = "<p style='color:#fff;padding:2rem'>HLS not supported.</p>";
  }}
}})();
</script>
</body>
</html>"""

    return HTMLResponse(content=html, headers={
        "Content-Security-Policy": "frame-ancestors *",
        "Cache-Control": "no-store",
    })


def _rewrite_media_m3u8(content: str, base: str, api_origin: str) -> str:
    """Rewrite a media playlist: proxy key URI and all segment URLs."""
    key_url = None
    explicit_iv = None

    km = re.search(r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', content)
    if km:
        raw_key = km.group(1)
        key_url = raw_key if raw_key.startswith("http") else f"{base}/{raw_key}"
    ivm = re.search(r'#EXT-X-KEY:[^\n]*IV=(0x[0-9a-fA-F]+)', content)
    if ivm:
        explicit_iv = ivm.group(1)

    seq = 0
    sm = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', content)
    if sm:
        seq = int(sm.group(1))

    lines_out = []
    seg_idx = seq
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-KEY"):
            # Skip — decryption is done server-side in /api/proxy/seg
            # so hls.js must NOT try to decrypt again
            pass
        elif s and not s.startswith("#"):
            seg_url = s if s.startswith("http") else f"{base}/{s}"
            ku = _up.quote(key_url or "", safe="")
            su = _up.quote(seg_url, safe="")
            iv_param = f"&iv={_up.quote(explicit_iv, safe='')}" if explicit_iv else f"&idx={seg_idx}"
            lines_out.append(f"{api_origin}/api/proxy/seg?url={su}&key={ku}{iv_param}")
            seg_idx += 1
        else:
            lines_out.append(line)

    return "\n".join(lines_out)


@app.get("/api/hlsjs")
def serve_hlsjs():
    """Serve hls.js from our own domain to avoid CDN tracking prevention blocks."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js", timeout=10) as r:
            js = r.read()
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch hls.js")
    return Response(content=js, media_type="application/javascript",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"})


@app.get("/api/proxy/m3u8")
def proxy_m3u8(url: str = Query(...)):
    """
    Fetch the real m3u8 and rewrite all URLs through our proxy.
    Handles both master playlists (with child m3u8 links) and media playlists.
    """
    api_origin = os.environ.get("API_ORIGIN", "https://apis.ayohost.site")
    resp = api._request(url)
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch m3u8")

    content = resp.read().decode("utf-8")
    base = url.rsplit("/", 1)[0]

    # Detect master playlist — contains #EXT-X-STREAM-INF lines
    if "#EXT-X-STREAM-INF" in content:
        # Rewrite child playlist URLs to go through our proxy
        lines_out = []
        for line in content.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                child_url = s if s.startswith("http") else f"{base}/{s}"
                proxied = f"{api_origin}/api/proxy/m3u8?url={_up.quote(child_url, safe='')}"
                lines_out.append(proxied)
            else:
                lines_out.append(line)
        rewritten = "\n".join(lines_out)
    else:
        rewritten = _rewrite_media_m3u8(content, base, api_origin)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


@app.get("/api/proxy/key")
def proxy_key(url: str = Query(...)):
    """Proxy the AES-128 decryption key."""
    resp = api._request(url)
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch key")
    return Response(
        content=resp.read(),
        media_type="application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/proxy/seg")
def proxy_seg(url: str = Query(...), key: str = Query(...), idx: int = Query(default=0), iv: str = Query(default="")):
    """Fetch encrypted .ts segment, decrypt it, serve clean video data."""
    seg_resp = api._request(url)
    if not seg_resp:
        raise HTTPException(status_code=502, detail="Failed to fetch segment")

    # _request uses preload_content=False — must read explicitly
    encrypted = seg_resp.read()
    if not encrypted:
        raise HTTPException(status_code=502, detail="Empty segment response")

    # No key = unencrypted stream, pass through directly
    if not key:
        return Response(
            content=encrypted,
            media_type="video/mp2t",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    key_resp = api._request(key)
    if not key_resp:
        raise HTTPException(status_code=502, detail="Failed to fetch key")
    aes_key = key_resp.read()[:16]

    # Use explicit IV from manifest if provided, otherwise derive from sequence idx
    if iv:
        iv_bytes = bytes.fromhex(iv.lstrip("0x").lstrip("0X").zfill(32))
    else:
        iv_bytes = idx.to_bytes(16, byteorder="big")

    # Pad to AES block boundary
    remainder = len(encrypted) % 16
    if remainder:
        encrypted += b"\0" * (16 - remainder)

    decrypted = AES.new(aes_key, AES.MODE_CBC, iv_bytes).decrypt(encrypted)

    # Strip PKCS7 padding if present
    if len(decrypted) > 0:
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16 and decrypted[-pad_len:] == bytes([pad_len] * pad_len):
            decrypted = decrypted[:-pad_len]

    return Response(
        content=decrypted,
        media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── Download jobs ────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    anime_slug: str
    episode_session: str
    anime_title: str
    episode_number: int
    quality: str = "best"
    audio: str = "jpn"


def _run_download(job_id: str, req: DownloadRequest):
    job = _get_job(job_id)
    try:
        job["status"] = "resolving"; _set_job(job_id, job)

        kwik_url = api.get_stream_url(req.anime_slug, req.episode_session, req.quality, req.audio)
        if not kwik_url:
            job["status"] = "failed"; job["error"] = "Could not resolve stream URL"
            _set_job(job_id, job); return

        m3u8_url = api.get_playlist_url(kwik_url)
        if not m3u8_url:
            job["status"] = "failed"; job["error"] = "Could not resolve playlist URL"
            _set_job(job_id, job); return

        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in req.anime_title)
        ep_dir = DOWNLOAD_DIR / safe / f"ep{req.episode_number}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        out_mp4 = DOWNLOAD_DIR / safe / f"{safe}_ep{req.episode_number}.mp4"

        if out_mp4.exists():
            job["status"] = "done"; job["progress"] = 100; job["file_path"] = str(out_mp4)
            _set_job(job_id, job); return

        job["status"] = "downloading"; _set_job(job_id, job)

        pl_path = downloader.fetch_playlist(m3u8_url, str(ep_dir))
        if not pl_path:
            job["status"] = "failed"; job["error"] = "Failed to fetch playlist"
            _set_job(job_id, job); return

        if not downloader.download_from_playlist_cli(pl_path, num_threads=8):
            job["status"] = "failed"; job["error"] = "Segment download failed"
            _set_job(job_id, job); return

        job["status"] = "compiling"; _set_job(job_id, job)

        def on_progress(pct: int):
            j = _get_job(job_id); j["progress"] = pct; _set_job(job_id, j)

        if not downloader.compile_video(str(ep_dir), str(out_mp4), on_progress):
            job["status"] = "failed"; job["error"] = "FFmpeg compilation failed"
            _set_job(job_id, job); return

        job["status"] = "done"; job["progress"] = 100; job["file_path"] = str(out_mp4)
        _set_job(job_id, job)

    except Exception as e:
        job = _get_job(job_id) or {}
        job["status"] = "failed"; job["error"] = str(e)
        _set_job(job_id, job)


@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    _set_job(job_id, {
        "job_id": job_id, "status": "queued", "progress": 0,
        "file_path": None, "error": None,
        "anime_title": req.anime_title, "episode_number": req.episode_number,
    })
    threading.Thread(target=_run_download, args=(job_id, req), daemon=True).start()
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
