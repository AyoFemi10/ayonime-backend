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

@app.get("/api/genre")
def get_by_genre(genre: str = Query(...), page: int = Query(default=1, ge=1)):
    """Search anime by genre using AnimePahe's search API."""
    import json as _json
    resp = api._request(f"https://animepahe.pw/api?m=search&q={_up.quote(genre)}&page={page}")
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch genre")
    data = _json.loads(resp.read())
    return {
        "data": data.get("data", []),
        "total": data.get("total", 0),
        "last_page": data.get("last_page", 1),
        "current_page": data.get("current_page", page),
    }

GENRES = [
    "Action", "Adventure", "Comedy", "Drama", "Fantasy",
    "Horror", "Mecha", "Music", "Mystery", "Psychological",
    "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"
]

@app.get("/api/genres")
def get_genres():
    return {"genres": GENRES}

@app.get("/api/app/version")
def get_app_version():
    """Returns the latest app version info for update checks."""
    return {
        "latest_version": "1.1.0",
        "release_date": "2026-04-23",
        "download_url": "https://ayonime.ayohost.site/download-app",
        "force_update_after_days": 10,
        "changelog": "Bug fixes: streaming, images, downloads to device, better UI",
    }


@app.get("/api/proxy/img")
def proxy_image(url: str = Query(...)):
    """Proxy anime poster images to bypass hotlink protection."""
    resp = api._request(url)
    if not resp:
        raise HTTPException(status_code=404, detail="Image not found")
    data = resp.read()
    content_type = "image/jpeg"
    return Response(content=data, media_type=content_type,
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"})


@app.get("/api/airing")
def get_airing():
    return {"data": api.check_for_updates()}

@app.get("/api/latest-release")
def get_recently_added(page: int = Query(default=1, ge=1)):
    """Paginated latest release anime — mirrors AnimePahe airing feed."""
    import json as _json
    from anime_downloader.utils import constants
    resp = api._request(f"{constants.AIRING_URL}&page={page}")
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch recently added")
    data = _json.loads(resp.read())
    return {
        "data": data.get("data", []),
        "total": data.get("total", 0),
        "per_page": data.get("per_page", 30),
        "current_page": data.get("current_page", page),
        "last_page": data.get("last_page", 1),
    }

@app.get("/api/top-anime")
def get_top_anime():
    """Top anime from AnimePahe — uses the anime list cache sorted by popularity."""
    import json as _json
    from anime_downloader.utils import constants
    # AnimePahe exposes a top list via m=top
    resp = api._request(f"{constants.BASE_URL}/api?m=top")
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch top anime")
    try:
        data = _json.loads(resp.read())
        return {"data": data.get("data", data) if isinstance(data, dict) else data}
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to parse top anime")

@app.get("/api/anime/{slug}/info")
def get_anime_info(slug: str, anime_name: str = Query(default="")):
    """Fetch anime metadata from AnimePahe search."""
    import json as _json
    from anime_downloader.utils import constants
    name = anime_name or slug
    resp = api._request(f"{constants.SEARCH_URL}&q={_up.quote(name)}")
    if not resp:
        raise HTTPException(status_code=502, detail="Failed to fetch anime info")
    data = _json.loads(resp.read())
    results = data.get("data", [])
    # Find best match
    match = next((r for r in results if r.get("session") == slug), results[0] if results else None)
    if not match:
        raise HTTPException(status_code=404, detail="Anime not found")
    return {"data": match}


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
    """Resolve stream → get m3u8 URL → return a proxied player URL."""
    kwik_url = api.get_stream_url(anime_slug, episode_session, quality, audio)
    if not kwik_url:
        raise HTTPException(status_code=404, detail="Stream not found")

    m3u8_url = api.get_playlist_url(kwik_url)
    if not m3u8_url:
        raise HTTPException(status_code=404, detail="Playlist not found")

    token = _up.quote(m3u8_url, safe="")
    return {"stream_url": f"/api/player?token={token}&_={uuid.uuid4().hex[:8]}", "playlist_url": None}


@app.get("/api/stream/qualities")
def get_stream_qualities(
    anime_slug: str = Query(...),
    episode_session: str = Query(...),
):
    """Return all available quality+audio combinations for an episode."""
    from bs4 import BeautifulSoup
    from anime_downloader.utils import constants

    play_url = f"{constants.PLAY_URL}/{anime_slug}/{episode_session}"
    response = api._request(play_url)
    if not response:
        raise HTTPException(status_code=404, detail="Episode page not found")

    soup = BeautifulSoup(response.read(), "html.parser")
    buttons = soup.find_all("button", attrs={"data-src": True, "data-av1": "0"})

    streams = []
    for b in buttons:
        q = b.get("data-resolution") or "0"
        a = b.get("data-audio") or "jpn"
        streams.append({"quality": q, "audio": a})

    streams.sort(key=lambda s: int(s["quality"]) if s["quality"].isdigit() else 9999, reverse=True)
    return {"streams": streams}


@app.get("/api/player")
def get_player(token: str = Query(...)):
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
html,body{{width:100%;height:100%;background:#000;overflow:hidden;font-family:system-ui,sans-serif}}
#wrap{{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#000}}
video{{width:100%;height:100%;object-fit:contain;display:block;cursor:pointer}}
#controls{{
  position:absolute;bottom:0;left:0;right:0;
  padding:12px 16px 14px;
  background:linear-gradient(transparent,rgba(0,0,0,.85));
  display:flex;flex-direction:column;gap:8px;
  opacity:0;transition:opacity .25s;
  pointer-events:none;
}}
#wrap:hover #controls,#wrap.show-controls #controls{{opacity:1;pointer-events:all}}
#progress-wrap{{position:relative;height:4px;background:rgba(255,255,255,.2);border-radius:4px;cursor:pointer}}
#progress-wrap:hover{{height:6px}}
#progress-buf{{position:absolute;left:0;top:0;height:100%;background:rgba(255,255,255,.3);border-radius:4px;width:0}}
#progress-bar{{position:absolute;left:0;top:0;height:100%;background:linear-gradient(90deg,#7c3aed,#ec4899);border-radius:4px;width:0}}
#progress-thumb{{
  position:absolute;top:50%;right:-6px;transform:translateY(-50%);
  width:12px;height:12px;border-radius:50%;background:#fff;
  opacity:0;transition:opacity .15s;box-shadow:0 0 6px rgba(124,58,237,.8);
}}
#progress-wrap:hover #progress-thumb{{opacity:1}}
#bottom{{display:flex;align-items:center;gap:10px}}
.btn{{background:none;border:none;cursor:pointer;color:#fff;padding:4px;display:flex;align-items:center;justify-content:center;opacity:.9;transition:opacity .15s}}
.btn:hover{{opacity:1}}
#time{{color:#fff;font-size:12px;font-weight:600;letter-spacing:.3px;white-space:nowrap}}
#vol-wrap{{display:flex;align-items:center;gap:6px}}
#vol-slider{{-webkit-appearance:none;appearance:none;width:70px;height:3px;border-radius:3px;background:rgba(255,255,255,.3);outline:none;cursor:pointer}}
#vol-slider::-webkit-slider-thumb{{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;background:#fff;cursor:pointer}}
#spacer{{flex:1}}
#spinner{{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  width:48px;height:48px;border-radius:50%;
  border:3px solid rgba(255,255,255,.15);border-top-color:#7c3aed;
  animation:spin .8s linear infinite;display:none;
}}
@keyframes spin{{to{{transform:translate(-50%,-50%) rotate(360deg)}}}}
#big-play{{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  width:64px;height:64px;border-radius:50%;
  background:rgba(124,58,237,.85);border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:transform .15s,background .15s;
}}
#big-play:hover{{transform:translate(-50%,-50%) scale(1.1);background:rgba(124,58,237,1)}}
#big-play svg{{margin-left:4px}}
</style>
</head>
<body>
<div id="wrap" class="show-controls">
  <video id="v" autoplay playsinline></video>
  <div id="spinner"></div>
  <button id="big-play">
    <svg width="24" height="24" fill="white" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
  </button>
  <div id="controls">
    <div id="progress-wrap">
      <div id="progress-buf"></div>
      <div id="progress-bar"><div id="progress-thumb"></div></div>
    </div>
    <div id="bottom">
      <button class="btn" id="btn-play">
        <svg id="ico-play" width="20" height="20" fill="white" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        <svg id="ico-pause" width="20" height="20" fill="white" viewBox="0 0 24 24" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      </button>
      <div id="vol-wrap">
        <button class="btn" id="btn-mute">
          <svg id="ico-vol" width="18" height="18" fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
          <svg id="ico-mute" width="18" height="18" fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24" style="display:none"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
        </button>
        <input type="range" id="vol-slider" min="0" max="1" step="0.05" value="1">
      </div>
      <span id="time">0:00 / 0:00</span>
      <div id="spacer"></div>
      <button class="btn" id="btn-fs">
        <svg id="ico-fs" width="18" height="18" fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
        <svg id="ico-exit-fs" width="18" height="18" fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24" style="display:none"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="10" y1="14" x2="3" y2="21"/><line x1="21" y1="3" x2="14" y2="10"/></svg>
      </button>
    </div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
(function(){{
  var origin = "{api_origin}";
  var src = origin + "/api/proxy/m3u8?url={encoded}";
  var video = document.getElementById("v");
  var wrap = document.getElementById("wrap");
  var spinner = document.getElementById("spinner");
  var bigPlay = document.getElementById("big-play");
  var btnPlay = document.getElementById("btn-play");
  var icoPlay = document.getElementById("ico-play");
  var icoPause = document.getElementById("ico-pause");
  var btnMute = document.getElementById("btn-mute");
  var icoVol = document.getElementById("ico-vol");
  var icoMute = document.getElementById("ico-mute");
  var volSlider = document.getElementById("vol-slider");
  var btnFs = document.getElementById("btn-fs");
  var icoFs = document.getElementById("ico-fs");
  var icoExitFs = document.getElementById("ico-exit-fs");
  var progressWrap = document.getElementById("progress-wrap");
  var progressBar = document.getElementById("progress-bar");
  var progressBuf = document.getElementById("progress-buf");
  var timeEl = document.getElementById("time");

  function fmt(s){{
    s = Math.floor(s||0);
    var m = Math.floor(s/60), sec = s%60;
    return m+":"+(sec<10?"0":"")+sec;
  }}

  function updatePlay(){{
    var paused = video.paused;
    icoPlay.style.display = paused?"block":"none";
    icoPause.style.display = paused?"none":"block";
    bigPlay.style.display = paused?"flex":"none";
  }}

  function updateTime(){{
    var pct = video.duration ? (video.currentTime/video.duration)*100 : 0;
    progressBar.style.width = pct+"%";
    timeEl.textContent = fmt(video.currentTime)+" / "+fmt(video.duration);
    // buffered
    if(video.buffered.length){{
      var bpct = (video.buffered.end(video.buffered.length-1)/video.duration)*100;
      progressBuf.style.width = bpct+"%";
    }}
  }}

  video.addEventListener("play", updatePlay);
  video.addEventListener("pause", updatePlay);
  video.addEventListener("timeupdate", updateTime);
  video.addEventListener("waiting", function(){{ spinner.style.display="block"; }});
  video.addEventListener("playing", function(){{ spinner.style.display="none"; }});
  video.addEventListener("canplay", function(){{ spinner.style.display="none"; }});

  // Click video = play/pause
  video.addEventListener("click", function(){{
    video.paused ? video.play() : video.pause();
  }});
  bigPlay.addEventListener("click", function(){{ video.play(); }});
  btnPlay.addEventListener("click", function(){{
    video.paused ? video.play() : video.pause();
  }});

  // Progress seek
  function seek(e){{
    var rect = progressWrap.getBoundingClientRect();
    var pct = Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));
    video.currentTime = pct * video.duration;
  }}
  var seeking = false;
  progressWrap.addEventListener("mousedown", function(e){{ seeking=true; seek(e); }});
  document.addEventListener("mousemove", function(e){{ if(seeking) seek(e); }});
  document.addEventListener("mouseup", function(){{ seeking=false; }});
  progressWrap.addEventListener("touchstart", function(e){{ seek(e.touches[0]); }});
  progressWrap.addEventListener("touchmove", function(e){{ e.preventDefault(); seek(e.touches[0]); }});

  // Volume
  volSlider.addEventListener("input", function(){{
    video.volume = parseFloat(volSlider.value);
    video.muted = video.volume === 0;
    icoVol.style.display = video.muted?"none":"block";
    icoMute.style.display = video.muted?"block":"none";
  }});
  btnMute.addEventListener("click", function(){{
    video.muted = !video.muted;
    icoVol.style.display = video.muted?"none":"block";
    icoMute.style.display = video.muted?"block":"none";
    volSlider.value = video.muted ? 0 : video.volume;
  }});

  // Fullscreen
  btnFs.addEventListener("click", function(){{
    if(!document.fullscreenElement){{
      wrap.requestFullscreen && wrap.requestFullscreen();
    }} else {{
      document.exitFullscreen && document.exitFullscreen();
    }}
  }});
  document.addEventListener("fullscreenchange", function(){{
    var fs = !!document.fullscreenElement;
    icoFs.style.display = fs?"none":"block";
    icoExitFs.style.display = fs?"block":"none";
  }});

  // Keyboard shortcuts
  document.addEventListener("keydown", function(e){{
    if(e.code==="Space"){{ e.preventDefault(); video.paused?video.play():video.pause(); }}
    if(e.code==="ArrowRight"){{ video.currentTime+=10; }}
    if(e.code==="ArrowLeft"){{ video.currentTime-=10; }}
    if(e.code==="ArrowUp"){{ video.volume=Math.min(1,video.volume+.1); volSlider.value=video.volume; }}
    if(e.code==="ArrowDown"){{ video.volume=Math.max(0,video.volume-.1); volSlider.value=video.volume; }}
    if(e.code==="KeyF"){{ btnFs.click(); }}
    if(e.code==="KeyM"){{ btnMute.click(); }}
  }});

  // Auto-hide controls
  var hideTimer;
  function showControls(){{
    wrap.classList.add("show-controls");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function(){{
      if(!video.paused) wrap.classList.remove("show-controls");
    }}, 3000);
  }}
  wrap.addEventListener("mousemove", showControls);
  wrap.addEventListener("touchstart", showControls);

  // HLS init
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
