from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pytube import YouTube
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
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
CACHE = {}
DEFAULT_CACHE_DURATION = 1800

def fetch_yt_data(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        yt = YouTube(url)
        
        formats = []
        for stream in yt.streams:
            formats.append({
                "itag": stream.itag,
                "ext": stream.mime_type.split('/')[-1],
                "resolution": stream.resolution,
                "fps": getattr(stream, 'fps', None),
                "vcodec": stream.video_codec,
                "acodec": stream.audio_codec,
                "url": stream.url,
                "filesize": stream.filesize,
                "type": stream.type
            })

        related_videos = []
        # pytubeの仕様上、initial_dataから関連動画を抽出
        try:
            results = yt.initial_data.get('contents', {}).get('twoColumnWatchNextResults', {}) \
                        .get('secondaryResults', {}).get('secondaryResults', {}).get('results', [])
            for res in results:
                v = res.get('compactVideoRenderer')
                if v:
                    related_videos.append({
                        "id": v.get('videoId'),
                        "title": v.get('title', {}).get('simpleText'),
                        "thumbnail": v.get('thumbnail', {}).get('thumbnails', [{}])[0].get('url'),
                        "uploader": v.get('shortBylineText', {}).get('runs', [{}])[0].get('text'),
                        "view_count": v.get('viewCountText', {}).get('simpleText')
                    })
        except Exception:
            pass

        return {
            "id": video_id,
            "title": yt.title,
            "description": yt.description,
            "thumbnail": yt.thumbnail_url,
            "duration": yt.length,
            "view_count": yt.views,
            "uploader": yt.author,
            "channel_id": yt.channel_id,
            "channel_url": yt.channel_url,
            "publish_date": str(yt.publish_date),
            "keywords": yt.keywords,
            "related_videos": related_videos,
            "formats": formats
        }
    except Exception as e:
        logger.error(f"Pytube error: {e}")
        raise e

@app.get("/api/v3/info/{video_id}")
async def get_video_info_v3(video_id: str):
    now = time.time()
    if video_id in CACHE:
        ts, data, dur = CACHE[video_id]
        if now - ts < dur:
            return data

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_yt_data, video_id)
        CACHE[video_id] = (now, info, DEFAULT_CACHE_DURATION)
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    info = await get_video_info_v3(video_id)
    return {"id": video_id, "formats": info["formats"]}

def download_and_merge(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    yt = YouTube(url)
    video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
    path = video.download(output_path="/tmp", filename=f"{video_id}.mp4")
    return path

def _cleanup_file(path: str):
    if os.path.exists(path):
        os.remove(path)

@app.get("/merge/{video_id}")
async def get_merged_stream(video_id: str):
    try:
        loop = asyncio.get_event_loop()
        path = await loop.run_in_executor(executor, download_and_merge, video_id)
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"{video_id}.mp4",
            background=BackgroundTask(_cleanup_file, path)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health_check():
    return {"status": "ok", "library": "pytube"}

@app.delete("/cache")
def clear_cache():
    CACHE.clear()
    return {"status": "cleared"}
