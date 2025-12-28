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

# ロギング設定
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

# 固定プロキシ（HTTPプロキシとして使用）
PROXY_URL = "https://443a8512-0cb2-46b3-8d4b-c3955ee3cc76-00-qvy8mcsrd7o.sisko.replit.dev"

CACHE = {}
DEFAULT_CACHE_DURATION = 1800
LONG_CACHE_DURATION = 14400

def get_ydl_opts():
    """yt-dlpオプション（HTTPプロキシ固定）"""
    return {
        "quiet": False,
        "no_warnings": False,
        "skip_download": True,
        "nocheckcertificate": True,
        "skip_live_postprocessor": True,
        "noplaylist": True,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,
        "proxy": PROXY_URL,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

def cleanup_cache():
    """期限切れキャッシュを削除"""
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]
    if expired:
        logger.info(f"Cleaned up {len(expired)} cache entries")

async def _fetch_and_cache_info(video_id: str):
    """動画情報を取得（HTTPプロキシ使用）"""
    current_time = time.time()
    cleanup_cache()
    
    # キャッシュチェック
    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            logger.info(f"Cache hit for {video_id}")
            return data
    
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = get_ydl_opts()
    
    logger.info(f"Fetching {video_id} via HTTP proxy: {PROXY_URL}")
    
    def fetch_info():
        try:
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"yt-dlp error: {str(e)}")
            raise
    
    try:
        loop = asyncio.get_event_loop()
        raw_info = await asyncio.wait_for(
            loop.run_in_executor(executor, fetch_info),
            timeout=60
        )
        
        # フォーマット情報を抽出
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
                "filesize": f.get("filesize"),
            }
            for f in raw_info.get("formats", [])
            if f.get("url") and f.get("ext") != "mhtml"
        ]
        
        response_data = {
            "title": raw_info.get("title"),
            "id": video_id,
            "duration": raw_info.get("duration"),
            "thumbnail": raw_info.get("thumbnail"),
            "description": raw_info.get("description"),
            "uploader": raw_info.get("uploader"),
            "formats": formats,
            "format_count": len(formats)
        }
        
        # キャッシュに保存
        cache_duration = (
            LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        )
        CACHE[video_id] = (current_time, response_data, cache_duration)
        
        logger.info(f"✓ Success for {video_id}: {len(formats)} formats (cached for {cache_duration}s)")
        
        return response_data
        
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching {video_id}")
        raise HTTPException(
            status_code=504,
            detail=f"Request timeout after 60 seconds"
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to fetch {video_id}: {error_msg}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch video info: {error_msg}"
        )

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    """動画ストリーム情報を取得"""
    return await _fetch_and_cache_info(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    """m3u8ストリーム情報を取得"""
    info_data = await _fetch_and_cache_info(video_id)
    
    m3u8_formats = [
        f for f in info_data["formats"]
        if f.get("url") and (
            ".m3u8" in f["url"] or 
            f.get("ext") == "m3u8" or 
            f.get("protocol") in ["m3u8_native", "http_dash_segments"]
        )
    ]
    
    if not m3u8_formats:
        raise HTTPException(status_code=404, detail="No m3u8 streams found")
    
    return {
        "title": info_data["title"],
        "id": video_id,
        "m3u8_formats": m3u8_formats
    }

@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):
    """最高品質のストリームを取得"""
    info_data = await _fetch_and_cache_info(video_id)
    formats = info_data["formats"]
    
    best_video = next(
        (f for f in sorted(formats, key=lambda x: x.get("vbr") or 0, reverse=True)
         if f.get("vcodec") not in ["none", None] and f.get("acodec") in ["none", None]),
        None
    )
    
    best_audio = next(
        (f for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True)
         if f.get("acodec") not in ["none", None] and f.get("vcodec") in ["none", None]),
        None
    )
    
    if not best_video and not best_audio:
        raise HTTPException(status_code=404, detail="No suitable streams found")
    
    return {
        "title": info_data["title"],
        "id": video_id,
        "best_video": best_video,
        "best_audio": best_audio,
        "note": "Combine streams using FFmpeg for best quality"
    }

def run_ytdlp_merge(video_id: str):
    """yt-dlpで動画と音声を結合してダウンロード"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = f"/tmp/{video_id}_%(title)s.%(ext)s"
    
    merge_opts = {
        "quiet": False,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "nocheckcertificate": True,
        "retries": 5,
        "proxy": PROXY_URL,
        "keep_videos": True,
    }
    
    try:
        with YoutubeDL(merge_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            safe_title = re.sub(r'[^\w\-_\. ]', '', info.get('title', video_id))
            
            # ダウンロードされたファイルを検索
            search_path = f"/tmp/{video_id}_*.mp4"
            files = glob.glob(search_path)
            
            if files:
                return max(files, key=os.path.getctime)
            else:
                raise Exception("yt-dlpがファイルを保存できませんでした")
    except Exception as e:
        logger.error(f"yt-dlp merge error: {e}")
        raise Exception(f"yt-dlpによる結合に失敗しました: {str(e)}")

def _cleanup_file(path: str):
    """ファイルを削除"""
    if os.path.exists(path):
        os.remove(path)
        logger.info(f"Cleaned up {path}")

@app.get("/merge/{video_id}")
async def get_merged_stream(video_id: str):
    """動画と音声を結合してダウンロード"""
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
        raise e
        
    except Exception as e:
        if output_file_path and os.path.exists(output_file_path):
            os.remove(output_file_path)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/proxy/info")
def proxy_info():
    """プロキシ情報を表示"""
    return {
        "proxy": PROXY_URL,
        "type": "HTTP",
        "cache_entries": len(CACHE)
    }

@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    """特定の動画のキャッシュを削除"""
    if video_id in CACHE:
        del CACHE[video_id]
        logger.info(f"Deleted cache for {video_id}")
        return {"status": "success", "message": f"Cache deleted for {video_id}"}
    raise HTTPException(status_code=404, detail="Cache entry not found")

@app.delete("/cache")
def clear_all_cache():
    """全キャッシュを削除"""
    count = len(CACHE)
    CACHE.clear()
    logger.info(f"Cleared all cache ({count} entries)")
    return {"status": "success", "message": f"Cleared {count} cache entries"}

@app.get("/cache")
def list_cache():
    """キャッシュ一覧を取得"""
    now = time.time()
    cleanup_cache()
    return {
        vid: {
            "title": data.get("title", "Unknown"),
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur,
            "format_count": data.get("format_count", 0)
        }
        for vid, (ts, data, dur) in CACHE.items()
    }

@app.get("/health")
def health_check():
    """ヘルスチェック"""
    return {
        "status": "ok",
        "proxy": PROXY_URL,
        "cache_entries": len(CACHE)
    }
