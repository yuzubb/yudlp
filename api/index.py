from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
import glob
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
PROXY_URL = "http://ytproxy-siawaseok.duckdns.org:3007"

CACHE = {}
DEFAULT_CACHE_DURATION = 1800
LONG_CACHE_DURATION = 14400

def get_ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "skip_download": True,
        "noplaylist": True,
        "proxy": PROXY_URL,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

def cleanup_cache():
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]

async def _fetch_and_cache_info(video_id: str, extended: bool = False):
    current_time = time.time()
    cleanup_cache()
    
    if video_id in CACHE and not extended:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data
    
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = get_ydl_opts()
    
    if extended:
        ydl_opts.update({
            "extract_flat": False,
            "force_generic_extractor": False,
        })

    def fetch_info():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    
    try:
        loop = asyncio.get_event_loop()
        raw_info = await asyncio.wait_for(
            loop.run_in_executor(executor, fetch_info),
            timeout=60
        )
        
        related_videos = []
        raw_related = raw_info.get("related_videos") or []
        for entry in raw_related:
            related_videos.append({
                "id": entry.get("id"),
                "title": entry.get("title"),
                "thumbnail": entry.get("thumbnail"),
                "uploader": entry.get("uploader") or entry.get("account_display_name"),
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
            })

        formats = [
            {
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec"),
                "url": f.get("url"),
                "filesize": f.get("filesize"),
            }
            for f in raw_info.get("formats", [])
            if f.get("url") and f.get("ext") != "mhtml"
        ]
        
        response_data = {
            "id": video_id,
            "title": raw_info.get("title"),
            "description": raw_info.get("description"),
            "thumbnail": raw_info.get("thumbnail"),
            "duration": raw_info.get("duration"),
            "view_count": raw_info.get("view_count"),
            "like_count": raw_info.get("like_count"),
            "upload_date": raw_info.get("upload_date"),
            "uploader": raw_info.get("uploader"),
            "uploader_id": raw_info.get("uploader_id"),
            "channel": raw_info.get("channel"),
            "channel_id": raw_info.get("channel_id"),
            "channel_url": raw_info.get("channel_url"),
            "subscribers": raw_info.get("channel_follower_count"),
            "tags": raw_info.get("tags"),
            "related_videos": related_videos,
            "formats": formats
        }
        
        CACHE[video_id] = (current_time, response_data, DEFAULT_CACHE_DURATION)
        return response_data
        
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v3/info/{video_id}")
async def get_video_info_v3(video_id: str):
    return await _fetch_and_cache_info(video_id, extended=True)

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    return await _fetch_and_cache_info(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    info_data = await _fetch_and_cache_info(video_id)
    m3u8_formats = [
        f for f in info_data["formats"]
        if f.get("url") and (".m3u8" in f["url"] or f.get("ext") == "m3u8")
    ]
    if not m3u8_formats:
        raise HTTPException(status_code=404, detail="No m3u8 streams found")
    return {"title": info_data["title"], "id": video_id, "m3u8_formats": m3u8_formats}

@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):
    info_data = await _fetch_and_cache_info(video_id)
    formats = info_data["formats"]
    best_video = next((f for f in sorted(formats, key=lambda x: x.get("vbr") or 0, reverse=True) if f.get("vcodec") != "none" and f.get("acodec") == "none"), None)
    best_audio = next((f for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True) if f.get("acodec") != "none" and f.get("vcodec") == "none"), None)
    return {"title": info_data["title"], "id": video_id, "best_video": best_video, "best_audio": best_audio}

def run_ytdlp_merge(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = f"/tmp/{video_id}_%(title)s.%(ext)s"
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "proxy": PROXY_URL,
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([url])
        files = glob.glob(f"/tmp/{video_id}_*.mp4")
        if not files: raise Exception("Download failed")
        return max(files, key=os.path.getctime)

def _cleanup_file(path: str):
    if os.path.exists(path): os.remove(path)

@app.get("/merge/{video_id}")
async def get_merged_stream(video_id: str):
    try:
        info = await _fetch_and_cache_info(video_id)
        loop = asyncio.get_event_loop()
        path = await loop.run_in_executor(executor, run_ytdlp_merge, video_id)
        return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4", background=BackgroundTask(_cleanup_file, path))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health_check():
    return {"status": "ok", "cache_entries": len(CACHE)}

@app.delete("/cache")
def clear_cache():
    CACHE.clear()
    return {"status": "cleared"}
