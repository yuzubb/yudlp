from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=20)

# --- yt-dlp 設定 ---
ydl_opts_base = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "socket_timeout": 10,
}

ydl_opts_video = {
    **ydl_opts_base,
    "extract_flat": False,
    "skip_download": True,
    "format": "bestvideo+bestaudio/best",
    "youtube_include_dash_manifest": True,
    "youtube_include_hls_manifest": True,
}

ydl_opts_flat = {
    **ydl_opts_base,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
}

# キャッシュ
VIDEO_CACHE = {}
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
PROCESSING_IDS = set()

def cleanup_cache():
    now = time.time()
    for c in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in c.items() if now - ts >= dur]
        for k in expired: del c[k]

# --- メインAPI ---

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    cleanup_cache()
    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    def fetch():
        with YoutubeDL(ydl_opts_video) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(video_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        formats = [
            {
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec"),
                "url": f.get("url")
            }
            for f in info.get("formats", [])
            if f.get("url") and f.get("ext") != "mhtml"
        ]

        response_data = {
            "title": info.get("title"),
            "id": video_id,
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            "formats": formats
        }
        
        dur = 14200 if len(formats) >= 12 else 600
        VIDEO_CACHE[video_id] = (time.time(), response_data, dur)
        return response_data
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_url(video_id: str):
    """m3u8 (HLS) のURLを直接取得してリダイレクト、または返却"""
    data = await get_streams(video_id)
    
    # 拡張子やプロトコルからm3u8を特定
    m3u8_url = None
    for f in data.get("formats", []):
        target_url = f.get("url", "")
        if ".m3u8" in target_url or f.get("ext") == "m3u8":
            m3u8_url = target_url
            break
    
    if m3u8_url:
        return RedirectResponse(url=m3u8_url)
    
    raise HTTPException(status_code=404, detail="m3u8 format not found for this video.")

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    cleanup_cache()
    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?list={playlist_id}" if playlist_id.startswith("RD") else f"https://www.youtube.com/playlist?list={playlist_id}"
    
    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(playlist_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{
            "id": e.get("id"),
            "title": e.get("title"),
            "uploader": e.get("uploader"),
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
        } for e in info.get("entries", []) if e]

        response_data = {"id": playlist_id, "title": info.get("title"), "video_count": len(entries), "entries": entries}
        PLAYLIST_CACHE[playlist_id] = (time.time(), response_data, 14200)
        return response_data
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(playlist_id)

@app.get("/channel/{channel_id}")
async def get_channel(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/{channel_id}/videos" if channel_id.startswith("@") else f"https://www.youtube.com/channel/{channel_id}/videos"

    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(channel_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        videos = [{
            "id": e.get("id"), "title": e.get("title"), "view_count": e.get("view_count"),
            "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
        } for e in info.get("entries", []) if e]

        response_data = {
            "channel_id": info.get("id"), "name": info.get("channel") or info.get("uploader"),
            "description": info.get("description"), "subscribers": info.get("subscriber_count"),
            "video_count": len(videos), "videos": videos
        }
        CHANNEL_CACHE[channel_id] = (time.time(), response_data, 86400)
        return response_data
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(channel_id)

# --- 管理用 ---

@app.get("/status")
def get_status():
    return {"is_processing": list(PROCESSING_IDS), "cache_stats": {"videos": len(VIDEO_CACHE), "playlists": len(PLAYLIST_CACHE), "channels": len(CHANNEL_CACHE)}}

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        "videos": {vid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for vid, (ts, _, dur) in VIDEO_CACHE.items()},
        "playlists": {pid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for pid, (ts, _, dur) in PLAYLIST_CACHE.items()},
        "channels": {cid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for cid, (ts, _, dur) in CHANNEL_CACHE.items()}
    }

@app.delete("/cache/clear")
def clear_cache():
    VIDEO_CACHE.clear()
    PLAYLIST_CACHE.clear()
    CHANNEL_CACHE.clear()
    return {"status": "ok"}
