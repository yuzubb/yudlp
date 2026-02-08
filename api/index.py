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

# 基本設定
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007"
}

# プレイリスト・チャンネル用設定
ydl_opts_flat = {
    **ydl_opts_base,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
}

# キャッシュと処理中管理
VIDEO_CACHE = {}      # { video_id: (timestamp, data, duration) }
PLAYLIST_CACHE = {}   # { playlist_id: (timestamp, data, duration) }
CHANNEL_CACHE = {}    # { channel_id: (timestamp, data, duration) }
PROCESSING_IDS = set()

DEFAULT_CACHE_DURATION = 600    # 10分
LONG_CACHE_DURATION = 14200     # 4時間

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

# --- ステータス確認API ---
@app.get("/status")
def get_status():
    """現在処理中のID一覧を返す"""
    return {"processing_count": len(PROCESSING_IDS), "processing_ids": list(PROCESSING_IDS)}

# --- 動画ストリーム情報 (既存の拡張) ---
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
            with YoutubeDL(ydl_opts_base) as ydl: return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)

        formats = [{
            "itag": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "fps": f.get("fps"),
            "url": f.get("url")
        } for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]

        res = {"title": info.get("title"), "id": video_id, "formats": formats}
        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (time.time(), res, dur)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(video_id)

# --- m3u8 取得用API ---
@app.get("/m3u8/{video_id}")
async def get_m3u8(video_id: str):
    """m3u8形式のストリームURLのみを抽出して返す"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    PROCESSING_IDS.add(video_id)
    
    try:
        def fetch():
            # m3u8(hls)が含まれるようにフォーマットを指定
            opts = {**ydl_opts_base, "format": "best"}
            with YoutubeDL(opts) as ydl: return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        # manifest_url または ext='m3u8' のものを抽出
        m3u8_streams = [
            {"url": f.get("url"), "resolution": f.get("resolution"), "vcodec": f.get("vcodec")}
            for f in info.get("formats", [])
            if "m3u8" in f.get("url", "") or f.get("ext") == "mp4" # yt-dlpのURL判定に準ずる
        ]

        if not m3u8_streams:
            # manifest_urlが直接ある場合（hls_nativeなど）
            hls_url = info.get("url") if "m3u8" in info.get("url", "") else None
            if hls_url: m3u8_streams = [{"url": hls_url, "resolution": "auto"}]

        return {"title": info.get("title"), "video_id": video_id, "m3u8_streams": m3u8_streams}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(video_id)

# --- プレイリスト情報 ---
@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    cleanup_cache()
    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/watch?list={playlist_id}" if playlist_id.startswith("RD") else f"https://www.youtube.com/playlist?list={playlist_id}"
    PROCESSING_IDS.add(playlist_id)
    
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl: return ydl.extract_info(url, download=False)
            
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title"), "uploader": e.get("uploader"), "duration": e.get("duration"), "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None} for e in info.get("entries", []) if e]
        res = {"id": playlist_id, "title": info.get("title"), "video_count": len(entries), "entries": entries}
        PLAYLIST_CACHE[playlist_id] = (time.time(), res, 14200)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(playlist_id)

# --- チャンネル情報 ---
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
            with YoutubeDL(ydl_opts_flat) as ydl: return ydl.extract_info(url, download=False)
            
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        videos = [{"id": e.get("id"), "title": e.get("title"), "view_count": e.get("view_count"), "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None} for e in info.get("entries", []) if e]
        res = {"channel_id": info.get("id"), "name": info.get("channel") or info.get("uploader"), "description": info.get("description"), "subscribers": info.get("subscriber_count"), "video_count": len(videos), "videos": videos}
        CHANNEL_CACHE[channel_id] = (time.time(), res, 86400)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(channel_id)
