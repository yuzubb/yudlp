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
VIDEO_CACHE = {}      # { id: (timestamp, data, duration) }
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
PROCESSING_IDS = set()

DEFAULT_CACHE_DURATION = 600    # 10分
LONG_CACHE_DURATION = 14200     # 4時間

def cleanup_cache():
    """期限切れのキャッシュをクリーンアップ"""
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

# --- システム・管理 API ---

@app.get("/status")
def get_status():
    """現在非同期処理中のID一覧を返す"""
    return {
        "processing_count": len(PROCESSING_IDS),
        "processing_ids": list(PROCESSING_IDS)
    }

@app.get("/api/2/cache")
def list_cache():
    """すべてのキャッシュ状況をカテゴリ別に表示"""
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
    """指定したIDのキャッシュを削除"""
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
        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (time.time(), res, dur)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8(video_id: str):
    """
    iOSのUser-Agentを使用してHLS(m3u8)マニフェストURLを抽出する
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    PROCESSING_IDS.add(video_id)
    try:
        def fetch():
            # HLSを誘発するためにモバイルUAを使用
            opts = {
                **ydl_opts_base,
                "user_agent": "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        # protocolがm3u8またはURLにm3u8が含まれるものを抽出
        streams = [
            {
                "url": f.get("url"),
                "resolution": f.get("resolution"),
                "protocol": f.get("protocol"),
                "ext": f.get("ext")
            }
            for f in info.get("formats", [])
            if f.get("protocol") == "m3u8_native" or ".m3u8" in f.get("url", "")
        ]

        # 取得できない場合のフォールバック
        if not streams and info.get("hls_url"):
            streams.append({
                "url": info.get("hls_url"),
                "resolution": "adaptive",
                "protocol": "m3u8_native",
                "ext": "m3u8"
            })

        return {
            "title": info.get("title"),
            "video_id": video_id,
            "m3u8_streams": streams
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

# --- プレイリスト / チャンネル API ---

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    cleanup_cache()
    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    if playlist_id.startswith("RD"):
        url = f"https://www.youtube.com/watch?list={playlist_id}"
    
    PROCESSING_IDS.add(playlist_id)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            } for e in info.get("entries", []) if e
        ]
        res = {"id": playlist_id, "title": info.get("title"), "video_count": len(entries), "entries": entries}
        PLAYLIST_CACHE[playlist_id] = (time.time(), res, 14200)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(playlist_id)

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
        videos = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "view_count": e.get("view_count")
            } for e in info.get("entries", []) if e
        ]
        res = {"channel_id": info.get("id"), "name": info.get("uploader") or info.get("channel"), "videos": videos}
        CHANNEL_CACHE[channel_id] = (time.time(), res, 86400)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)
