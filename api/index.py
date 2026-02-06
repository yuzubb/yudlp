from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# --- CORS設定 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor()

# --- yt-dlp 共通設定 ---
ydl_opts_base = {
    "quiet": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "extract_flat": True,
}

# 1. 動画ストリーミング用
ydl_opts_video = {
    **ydl_opts_base,
    "extract_flat": False, # ストリーミングURLが必要なのでFalse
    "skip_download": True,
    "format": "bestvideo+bestaudio/best",
}

# 2. プレイリスト/チャンネル用
ydl_opts_flat = {
    **ydl_opts_base,
    "extract_flat": "in_playlist", # ミックスリスト対応
    "dump_single_json": True,
}

# --- キャッシュ管理 ---
# 形式: { id: (timestamp, data, duration) }
VIDEO_CACHE = {}
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}

PROCESSING_IDS = set()
DEFAULT_CACHE_DURATION = 600    # 10分
LONG_CACHE_DURATION = 14200     # 約4時間
CHANNEL_CACHE_DURATION = 86400  # 24時間

def cleanup_cache():
    """期限切れのキャッシュを一括削除"""
    now = time.time()
    for cache_dict in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in cache_dict.items() if now - ts >= dur]
        for k in expired:
            del cache_dict[k]

# --- APIエンドポイント ---

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    """動画のストリーミングURLとメタデータを取得"""
    current_time = time.time()
    cleanup_cache()

    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if current_time - ts < dur:
            return data

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
            "formats": formats
        }

        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (current_time, response_data, dur)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    """プレイリスト内の動画一覧を取得（ミックスリスト対応）"""
    current_time = time.time()
    cleanup_cache()

    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if current_time - ts < dur:
            return data

    # RDから始まるミックスリストと通常のPLリストを判定
    if playlist_id.startswith("RD"):
        url = f"https://www.youtube.com/watch?list={playlist_id}"
    else:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"

    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(playlist_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)

        entries = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "uploader": e.get("uploader"),
                "duration": e.get("duration"),
                "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            }
            for e in info.get("entries", []) if e
        ]

        response_data = {
            "id": playlist_id,
            "title": info.get("title"),
            "video_count": len(entries),
            "entries": entries
        }

        PLAYLIST_CACHE[playlist_id] = (current_time, response_data, LONG_CACHE_DURATION)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(playlist_id)

@app.get("/channel/{channel_id}")
async def get_channel(channel_id: str):
    """チャンネルの基本情報とアップロード動画を取得"""
    current_time = time.time()
    cleanup_cache()

    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if current_time - ts < dur:
            return data

    # ハンドル(@name)かIDかを判定
    url = f"https://www.youtube.com/{channel_id}" if channel_id.startswith("@") else f"https://www.youtube.com/channel/{channel_id}"

    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(channel_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)

        videos = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "view_count": e.get("view_count"),
                "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            }
            for e in info.get("entries", []) if e
        ]

        response_data = {
            "channel_id": info.get("id"),
            "name": info.get("channel") or info.get("uploader"),
            "description": info.get("description"),
            "subscribers": info.get("subscriber_count"),
            "thumbnails": info.get("thumbnails"),
            "videos": videos[:50] # 最新50件
        }

        CHANNEL_CACHE[channel_id] = (current_time, response_data, CHANNEL_CACHE_DURATION)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)

# --- 管理用API ---

@app.get("/status")
def get_status():
    return {
        "is_processing": list(PROCESSING_IDS),
        "cache_stats": {
            "videos": len(VIDEO_CACHE),
            "playlists": len(PLAYLIST_CACHE),
            "channels": len(CHANNEL_CACHE)
        }
    }

@app.delete("/cache/clear")
def clear_all_cache():
    VIDEO_CACHE.clear()
    PLAYLIST_CACHE.clear()
    CHANNEL_CACHE.clear()
    return {"message": "All cache cleared"}
