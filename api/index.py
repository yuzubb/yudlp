from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
import re
import glob

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor()

PROXY_URL = "http://yt-kwuf.onrender.com"

class MyLogger:
    def debug(self, msg):
        print(f"[yt-dlp DEBUG] {msg}")
    def warning(self, msg):
        print(f"[yt-dlp WARNING] {msg}")
    def error(self, msg):
        print(f"[yt-dlp ERROR] {msg}")

ydl_opts = {
    "quiet": False,
    "verbose": True,
    "logger": MyLogger(),
    "skip_download": True,
    "nocheckcertificate": True,
    "proxy": PROXY_URL,
    "impersonate": "chrome110",
    "skip_live_postprocessor": True,
    "noplaylist": True,
    "getdescription": False,
    "getduration": False,
    "getcomments": False
}

CACHE = {}
DEFAULT_CACHE_DURATION = 1800
LONG_CACHE_DURATION = 14200

def cleanup_cache():
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]

async def _fetch_and_cache_info(video_id: str):
    current_time = time.time()
    cleanup_cache()

    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_event_loop()
        raw_info = await loop.run_in_executor(executor, fetch_info)

        formats = [
            {
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec"),
                "url": f.get("url"),
                "protocol": f.get("protocol"),
                "vbr": f.get("vbr"),
                "abr": f.get("abr"),
            }
            for f in raw_info.get("formats", [])
            if f.get("url") and f.get("ext") != "mhtml"
        ]

        response_data = {
            "title": raw_info.get("title"),
            "id": video_id,
            "formats": formats
        }

        cache_duration = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        CACHE[video_id] = (current_time, response_data, cache_duration)

        return response_data

    except Exception as e:
        print(f"Error fetching {video_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch stream info: {str(e)}")

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    return await _fetch_and_cache_info(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    try:
        info_data = await _fetch_and_cache_info(video_id)
    except HTTPException as e:
        raise e

    m3u8_formats = [
        f for f in info_data["formats"]
        if f.get("url") and (
            ".m3u8" in f["url"]
            or f.get("ext") == "m3u8"
            or f.get("protocol") in ["m3u8_native", "http_dash_segments"]
        )
    ]

    if not m3u8_formats:
        raise HTTPException(status_code=404, detail="No manifest found.")

    return {
        "title": info_data["title"],
        "id": video_id,
        "m3u8_formats": m3u8_formats
    }

@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):
    try:
        info_data = await _fetch_and_cache_info(video_id)
    except HTTPException as e:
        raise e

    formats = info_data["formats"]
    best_video = next((f for f in sorted(formats, key=lambda x: x.get("vbr") or 0, reverse=True) if f.get("vcodec") not in ["none", None] and f.get("acodec") in ["none", None]), None)
    best_audio = next((f for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True) if f.get("acodec") not in ["none", None] and f.get("vcodec") in ["none", None]), None)

    if not best_video and not best_audio:
        raise HTTPException(status_code=404, detail="Best streams not found.")

    return {
        "title": info_data["title"],
        "id": video_id,
        "best_video": best_video,
        "best_audio": best_audio
    }

def run_ytdlp_merge(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = f"/tmp/{video_id}_%(title)s.%(ext)s"

    merge_opts = {
        "verbose": True,
        "logger": MyLogger(),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "nocheckcertificate": True,
        "proxy": PROXY_URL,
        "impersonate": "chrome",
        "keep_videos": True,
    }

    try:
        with YoutubeDL(merge_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            files = glob.glob(f"/tmp/{video_id}_*.mp4")
            if files:
                return max(files, key=os.path.getctime)
            else:
                raise Exception("File not saved.")
    except Exception as e:
        raise Exception(f"Merge failed: {str(e)}")

def _cleanup_file(path: str):
    if os.path.exists(path):
        os.remove(path)

@app.get("/merge/{video_id}")
async def get_merged_stream(video_id: str):
    output_file_path = None
    try:
        info = await _fetch_and_cache_info(video_id)
        title = info.get("title", video_id)
        loop = asyncio.get_event_loop()
        output_file_path = await loop.run_in_executor(executor, run_ytdlp_merge, video_id)
        safe_title = re.sub(r'[^\w\-_\. ]', '', title.replace(' ', '_'))[:50]

        return FileResponse(
            output_file_path,
            media_type="video/mp4",
            filename=f"{video_id}_{safe_title}.mp4",
            background=BackgroundTask(_cleanup_file, output_file_path)
        )
    except Exception as e:
        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    if video_id in CACHE:
        del CACHE[video_id]
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/cache")
def list_cache():
    now = time.time()
    cleanup_cache()
    return {vid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for vid, (ts, _, dur) in CACHE.items()}
