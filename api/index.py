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

# 並列処理のワーカー数を維持
executor = ThreadPoolExecutor(max_workers=30)

ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "ignore_no_formats_error": True,
    "ignoreerrors": True,
    "socket_timeout": 7
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

    try:
        # 1. まず一覧を25件分高速取得
        def fetch_list():
            opts = {**ydl_opts_base, "extract_flat": True, "playlist_items": "1-25"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        list_info = await loop.run_in_executor(executor, fetch_list)
        if not list_info or "entries" not in list_info:
            return {"channel": channel_id, "streams": []}

        # 2. 25件すべてを並列で詳細解析（視聴者数・正確なステータスを取得）
        raw_entries = list_info["entries"][:25]

        def fetch_detail(entry):
            if not entry: return None
            try:
                # 各動画のページを個別に叩いて最新情報を取得
                with YoutubeDL(ydl_opts_base) as ydl:
                    return ydl.extract_info(f"https://www.youtube.com/watch?v={entry['id']}", download=False)
            except:
                return entry

        # 並列実行
        detail_tasks = [loop.run_in_executor(executor, fetch_detail, e) for e in raw_entries]
        detailed_results = await asyncio.gather(*detail_tasks)

        streams = []
        for e in detailed_results:
            if not e: continue
            
            # ステータスの詳細判定
            status_raw = e.get("live_status")
            is_live = status_raw == "live"
            # yt-dlpのプロパティから予定枠か判定
            is_upcoming = status_raw == "upcoming" or e.get("availability") == "upcoming" or (not is_live and e.get("release_timestamp"))
            
            # 視聴者数(live) or 待機人数(upcoming)
            viewers = e.get("concurrent_view_count") or e.get("waiting_count") or 0

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

        res = {"channel": list_info.get("uploader") or channel_id, "streams": streams}
        # 配信情報は頻繁に変わるため、キャッシュは3分(180秒)に設定
        STREAMS_CACHE[channel_id] = (time.time(), res, 180)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
            opts = {**ydl_opts_base, "extract_flat": True, "playlist_items": "1-25"}
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
            opts = {**ydl_opts_base, "extract_flat": True, "playlist_items": "1-25"}
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
            opts = {**ydl_opts_base, "extract_flat": True, "playlist_items": "1-25"}
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
