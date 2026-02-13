from fastapi import FastAPI, HTTPException, Query
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 25件を同時に処理するためにスレッド数を調整
executor = ThreadPoolExecutor(max_workers=30)

# 安定性を重視したベースオプション
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "ignore_no_formats_error": True, # 配信前動画のエラー回避
    "ignoreerrors": True,           # 1件の失敗で全体を止めない
    "no_warnings": True,
    "extract_flat": False,          # 詳細情報を最初から取りに行く
}

VIDEO_CACHE = {}      
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
STREAMS_CACHE = {}
PROCESSING_IDS = set()

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

def get_best_thumbnail(thumbnails):
    if not thumbnails: return None
    return thumbnails[-1].get("url")

@app.get("/status")
def get_status():
    return {
        "status": "operational",
        "processing_count": len(PROCESSING_IDS),
        "cache_stats": {
            "videos": len(VIDEO_CACHE),
            "playlists": len(PLAYLIST_CACHE),
            "channels": len(CHANNEL_CACHE),
            "streams": len(STREAMS_CACHE)
        }
    }

@app.get("/cache")
def get_cache_info():
    return {
        "video": list(VIDEO_CACHE.keys()),
        "playlist": list(PLAYLIST_CACHE.keys()),
        "channel": list(CHANNEL_CACHE.keys()),
        "streams": list(STREAMS_CACHE.keys())
    }

@app.delete("/cache")
def clear_cache():
    for c in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE]: c.clear()
    return {"message": "Caches cleared"}

@app.get("/channel/streams/{channel_id}")
async def get_channel_streams(channel_id: str):
    cleanup_cache()
    if channel_id in STREAMS_CACHE:
        ts, data, dur = STREAMS_CACHE[channel_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/channel/{channel_id}/streams" if channel_id.startswith("UC") else f"https://www.youtube.com/{channel_id}/streams"

    PROCESSING_IDS.add(f"streams_{channel_id}")
    try:
        def fetch_detailed_list():
            # playlist_itemsで25個に絞り、詳細抽出モードで実行
            opts = {
                **ydl_opts_base,
                "playlist_items": "1-25",
            }
            with YoutubeDL(opts) as ydl:
                # チャンネルの配信一覧を「動画1件ずつ詳細解析しながら」取得
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_detailed_list)
        
        if not info or "entries" not in info:
            return {"channel": channel_id, "streams": []}

        streams = []
        for e in info["entries"]:
            if not e: continue
            
            # ステータス判定の強化
            ls = e.get("live_status")
            is_live = ls == "live"
            # release_timestampがある、またはステータスがupcomingなら予定
            is_upcoming = ls == "upcoming" or e.get("availability") == "upcoming" or (not is_live and e.get("release_timestamp") is not None)
            
            # ライブなら同時接続数、予定なら待機人数、それ以外は0
            viewers = 0
            if is_live:
                viewers = e.get("concurrent_view_count") or 0
            elif is_upcoming:
                viewers = e.get("waiting_count") or 0

            streams.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "status": "live" if is_live else "upcoming" if is_upcoming else "archived",
                "viewers": int(viewers),
                "scheduled_start": e.get("release_timestamp") or e.get("timestamp"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "is_live": is_live,
                "is_upcoming": is_upcoming
            })

        res = {"channel": info.get("uploader") or info.get("title"), "streams": streams}
        STREAMS_CACHE[channel_id] = (time.time(), res, 120) # 安定性重視のためキャッシュは2分
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(f"streams_{channel_id}")

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    cleanup_cache()
    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        def fetch():
            with YoutubeDL(ydl_opts_base) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        if not info: raise Exception("Info fetch failed")
        
        formats = [{"itag": f.get("format_id"), "ext": f.get("ext"), "resolution": f.get("resolution"), "url": f.get("url")} 
                   for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]
        res = {"title": info.get("title"), "id": video_id, "formats": formats}
        VIDEO_CACHE[video_id] = (time.time(), res, 600)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    try:
        def fetch():
            opts = {**ydl_opts_base, "playlist_items": "1-25"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title"), "thumbnail": get_best_thumbnail(e.get("thumbnails"))} for e in info.get("entries", []) if e]
        return {"id": playlist_id, "title": info.get("title"), "entries": entries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/channel/{channel_id}")
async def get_channel_videos(channel_id: str):
    base_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id.startswith("UC") else f"https://www.youtube.com/{channel_id}"
    try:
        def fetch():
            opts = {**ydl_opts_base, "playlist_items": "1-25"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(f"{base_url}/videos", download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        return {"channel_id": channel_id, "videos": [{"id": e.get("id"), "title": e.get("title")} for e in info.get("entries", []) if e]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/short/{channel_id}")
async def get_shorts(channel_id: str):
    url = f"https://www.youtube.com/{channel_id}/shorts"
    try:
        def fetch():
            opts = {**ydl_opts_base, "playlist_items": "1-25"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        return {"shorts": [{"id": e.get("id"), "title": e.get("title")} for e in info.get("entries", []) if e]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
