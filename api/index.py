from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
import subprocess
import glob
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor()
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "proxy": "http://yt-kwuf.onrender.com",
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
    print(f"--- Cache Cleanup: Removed {len(expired)} entries. ---")

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

        cache_duration = (
            LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        )

        CACHE[video_id] = (current_time, response_data, cache_duration)

        print(f"{video_id} の {cache_duration}秒キャッシュを作成しました。URL数: {len(formats)}")

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
        raise HTTPException(status_code=404, detail="m3u8 または DASH 形式のストリームマニフェストは見つかりませんでした。")

    m3u8_response = {
        "title": info_data["title"],
        "id": video_id,
        "m3u8_formats": m3u8_formats
    }

    return m3u8_response


@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):

    try:
        info_data = await _fetch_and_cache_info(video_id)
    except HTTPException as e:
        raise e

    formats = info_data["formats"]

    best_video_format = next(
        (
            f for f in sorted(formats, key=lambda x: x.get("vbr") or 0, reverse=True)
            if f.get("vcodec") not in ["none", None] and f.get("acodec") in ["none", None]
        ),
        None
    )

    best_audio_format = next(
        (
            f for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True)
            if f.get("acodec") not in ["none", None] and f.get("vcodec") in ["none", None]
        ),
        None
    )

    if not best_video_format and not best_audio_format:
        raise HTTPException(status_code=404, detail="最高品質の動画ストリームまたは音声ストリームが見つかりませんでした。")

    high_response = {
        "title": info_data["title"],
        "id": video_id,
        "best_video": best_video_format,
        "best_audio": best_audio_format,
        "note": "NOTE: To achieve best quality, you must combine 'best_video' and 'best_audio' streams using a tool like FFmpeg, as they are separate streams (DASH/HLS)."
    }

    return high_response

def run_ytdlp_merge(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"

    output_template = f"/tmp/{video_id}_%(title)s.%(ext)s"

    merge_opts = {
        "quiet": False,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "nocheckcertificate": True,
        "retries": 5,
        "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
        "keep_videos": True,
    }

    try:
        with YoutubeDL(merge_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            safe_title = re.sub(r'[^\w\-_\. ]', '', info.get('title', video_id))
            final_filename_template = f"/tmp/{video_id}_{safe_title}.mp4"

            search_path = f"/tmp/{video_id}_*.mp4"
            files = glob.glob(search_path)

            if files:
                return max(files, key=os.path.getctime)
            else:
                raise Exception("yt-dlpがファイルを保存できませんでした。")

    except Exception as e:
        print(f"yt-dlp merge error: {e}")
        raise Exception(f"yt-dlpによる結合に失敗しました: {str(e)}")

def _cleanup_file(path: str):
    if os.path.exists(path):
        os.remove(path)
        print(f"Cleaned up {path} after response.")


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

    except HTTPException as e:
        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)
            print(f"Cleaned up {output_file_path} after HTTPException.")
        raise e
    except Exception as e:
        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)
            print(f"Cleaned up {output_file_path} after Exception.")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    if video_id in CACHE:
        del CACHE[video_id]
        print(f"{video_id} のキャッシュを削除しました。")
        return {"status": "success", "message": f"{video_id} のキャッシュを削除しました。"}
    else:
        raise HTTPException(status_code=404, detail="指定されたIDのキャッシュは存在しません。")

@app.get("/cache")
def list_cache():
    now = time.time()
    cleanup_cache()
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
