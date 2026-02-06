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

# 基本的な ydl オプション
ydl_opts_base = {
    "quiet": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007"
}

# 動画用のオプション
ydl_opts_video = {
    **ydl_opts_base,
    "skip_download": True,
    "format": "bestvideo+bestaudio/best",
}

# プレイリスト用のオプション
ydl_opts_playlist = {
    **ydl_opts_base,
    "extract_flat": True,  # 動画のメタデータのみを取得（高速化）
    "dump_single_json": True,
}

# キャッシュと処理中リスト
CACHE = {}
PLAYLIST_CACHE = {}  # プレイリスト専用キャッシュ
PROCESSING_IDS = set()
DEFAULT_CACHE_DURATION = 600
LONG_CACHE_DURATION = 14200

def cleanup_cache():
    now = time.time()
    # 動画キャッシュのクリーンアップ
    expired_v = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired_v: del CACHE[vid]
    # プレイリストキャッシュのクリーンアップ
    expired_p = [pid for pid, (ts, _, dur) in PLAYLIST_CACHE.items() if now - ts >= dur]
    for pid in expired_p: del PLAYLIST_CACHE[pid]

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    current_time = time.time()
    cleanup_cache()

    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        with YoutubeDL(ydl_opts_video) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(video_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_info)

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
            "formats": formats
        }

        cache_duration = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        CACHE[video_id] = (current_time, response_data, cache_duration)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if video_id in PROCESSING_IDS:
            PROCESSING_IDS.remove(video_id)

# --- プレイリスト情報取得用API ---
@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    current_time = time.time()
    cleanup_cache()

    if playlist_id in PLAYLIST_CACHE:
        timestamp, data, duration = PLAYLIST_CACHE[playlist_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/playlist?list={playlist_id}"

    def fetch_playlist_info():
        with YoutubeDL(ydl_opts_playlist) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(playlist_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_playlist_info)

        entries = [
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "url": f"https://www.youtube.com/watch?v={entry.get('id')}",
                "thumbnail": entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None,
                "duration": entry.get("duration")
            }
            for entry in info.get("entries", [])
            if entry  # 削除された動画などを除外
        ]

        response_data = {
            "playlist_id": playlist_id,
            "title": info.get("title"),
            "video_count": len(entries),
            "entries": entries
        }

        # プレイリストは変更が少ないことが多いため、長めのキャッシュ時間を設定
        PLAYLIST_CACHE[playlist_id] = (current_time, response_data, LONG_CACHE_DURATION)
        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if playlist_id in PROCESSING_IDS:
            PROCESSING_IDS.remove(playlist_id)

# --- ステータス・管理用API ---
@app.get("/status")
def get_status():
    return {
        "processing_count": len(PROCESSING_IDS),
        "video_cache_count": len(CACHE),
        "playlist_cache_count": len(PLAYLIST_CACHE)
    }

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        "videos": {
            vid: {"age": int(now - ts), "remaining": int(dur - (now - ts))}
            for vid, (ts, _, dur) in CACHE.items()
        },
        "playlists": {
            pid: {"age": int(now - ts), "remaining": int(dur - (now - ts))}
            for pid, (ts, _, dur) in PLAYLIST_CACHE.items()
        }
    }
