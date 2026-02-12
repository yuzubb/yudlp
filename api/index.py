from fastapi import FastAPI, HTTPException, Query
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

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

# --- yt-dlp 基本設定 ---
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007"
}

ydl_opts_flat = {
    **ydl_opts_base,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
}

# --- キャッシュ & 処理中管理 ---
# 構造: { id: (取得時刻, データ本体, 有効期間秒) }
VIDEO_CACHE = {}      
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
PROCESSING_IDS = set()

# キャッシュ時間設定
DEFAULT_CACHE_DURATION = 600    # 10分
LONG_CACHE_DURATION = 14200     # 4時間
CHANNEL_CACHE_DURATION = 86400  # 24時間

def cleanup_cache():
    """期限切れのキャッシュを削除"""
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

def get_best_thumbnail(thumbnails):
    if not thumbnails: return None
    return thumbnails[-1].get("url")

# --- システム・管理 API ---

@app.get("/status")
def get_status():
    return {
        "processing_count": len(PROCESSING_IDS),
        "processing_ids": list(PROCESSING_IDS)
    }

@app.get("/api/2/cache")
def list_cache():
    now = time.time()
    def format_map(c):
        return {
            k: {
                "age_sec": int(now - v[0]),
                "remaining_sec": int(v[2] - (now - v[0])),
                "total_duration": v[2]
            } for k, v in c.items()
        }
    return {
        "video_streams": format_map(VIDEO_CACHE),
        "playlists": format_map(PLAYLIST_CACHE),
        "channels": format_map(CHANNEL_CACHE)
    }

@app.delete("/api/2/cache/{item_id}")
def delete_cache(item_id: str):
    deleted = False
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        if item_id in cache:
            del cache[item_id]
            deleted = True
    if deleted:
        return {"status": "success", "message": f"ID: {item_id} のキャッシュを削除しました。"}
    raise HTTPException(status_code=404, detail="キャッシュが存在しません。")

# --- メイン API (動画 / m3u8) ---

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    cleanup_cache()
    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    PROCESSING_IDS.add(video_id)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_base) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        formats = [{
            "itag": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "url": f.get("url")
        } for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]

        res = {"title": info.get("title"), "id": video_id, "formats": formats}
        # フォーマット数が多い場合は人気動画とみなし、長めにキャッシュ
        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (time.time(), res, dur)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    PROCESSING_IDS.add(video_id)
    try:
        def fetch():
            opts = {**ydl_opts_base, "user_agent": "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        streams = [{"url": f.get("url"), "resolution": f.get("resolution"), "protocol": f.get("protocol"), "ext": f.get("ext")}
                   for f in info.get("formats", []) if f.get("protocol") == "m3u8_native" or ".m3u8" in f.get("url", "")]

        if not streams and info.get("hls_url"):
            streams.append({"url": info.get("hls_url"), "resolution": "adaptive", "protocol": "m3u8_native", "ext": "m3u8"})

        return {"title": info.get("title"), "video_id": video_id, "m3u8_streams": streams}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

# --- プレイリスト / チャンネル / ショート API ---

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str, v: Optional[str] = Query(None)):
    cleanup_cache()
    cache_key = f"{playlist_id}_{v}" if v else playlist_id
    if cache_key in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[cache_key]
        if time.time() - ts < dur: return data
    
    if playlist_id.startswith("RD"):
        url = f"https://www.youtube.com/watch?v={v}&list={playlist_id}" if v else f"https://www.youtube.com/watch?list={playlist_id}"
    else:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
    
    PROCESSING_IDS.add(playlist_id)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title"), "thumbnail": get_best_thumbnail(e.get("thumbnails"))}
                   for e in info.get("entries", []) if e]
        
        res = {"id": playlist_id, "title": info.get("title"), "video_count": len(entries), "entries": entries}
        cache_dur = 7200 if playlist_id.startswith("RD") else LONG_CACHE_DURATION
        PLAYLIST_CACHE[cache_key] = (time.time(), res, cache_dur)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(playlist_id)

@app.get("/short/{channel_id}")
async def get_shorts(channel_id: str):
    cleanup_cache()
    cache_key = f"shorts_{channel_id}"
    if cache_key in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[cache_key]
        if time.time() - ts < dur: return data

    base_path = channel_id if channel_id.startswith("@") else f"channel/{channel_id}"
    url = f"https://www.youtube.com/{base_path}/shorts"
    
    PROCESSING_IDS.add(cache_key)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        shorts = [{"id": e.get("id"), "title": e.get("title"), "thumbnail": get_best_thumbnail(e.get("thumbnails")), "view_count": e.get("view_count")}
                  for e in info.get("entries", []) if e]
        
        res = {"channel_id": info.get("id"), "name": info.get("uploader") or info.get("channel"), "shorts": shorts}
        CHANNEL_CACHE[cache_key] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(cache_key)

@app.get("/channel/{channel_id}")
async def get_channel(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/{channel_id}/videos" if channel_id.startswith("@") else f"https://www.youtube.com/channel/{channel_id}/videos"
    
    PROCESSING_IDS.add(channel_id)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        # メタデータの抽出
        thumbnails = info.get("thumbnails", [])
        res = {
            "channel_id": info.get("id"),
            "name": info.get("uploader") or info.get("channel"),
            "description": info.get("description"),
            "subscriber_count": info.get("subscriber_count"),
            "avatar": thumbnails[0].get("url") if thumbnails else None,
            "banner": thumbnails[-1].get("url") if thumbnails else None,
            "videos": [{"id": e.get("id"), "title": e.get("title"), "view_count": e.get("view_count"), 
                        "thumbnail": get_best_thumbnail(e.get("thumbnails")), "duration": e.get("duration")}
                       for e in info.get("entries", []) if e]
        }
        
        CHANNEL_CACHE[channel_id] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

