from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=20)

# --- yt-dlp 設定 ---
ydl_opts_base = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "socket_timeout": 10,
}

# マニフェスト取得を最優先にする設定
ydl_opts_video = {
    **ydl_opts_base,
    "extract_flat": False,
    "skip_download": True,
    "format": "bestvideo+bestaudio/best",
    "youtube_include_dash_manifest": True,
    "youtube_include_hls_manifest": True,
    "noplaylist": True,
}

ydl_opts_flat = {
    **ydl_opts_base,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
}

# キャッシュストレージ
VIDEO_CACHE = {}
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
PROCESSING_IDS = set()

def cleanup_cache():
    now = time.time()
    for c in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in c.items() if now - ts >= dur]
        for k in expired: del c[k]

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    cleanup_cache()
    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    
    def fetch():
        with YoutubeDL(ydl_opts_video) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(video_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        raw_formats = info.get("formats", [])
        
        # 1. 通常のストリーミングフォーマットの抽出
        formats = [
            {
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec"),
                "url": f.get("url"),
                "protocol": f.get("protocol")
            }
            for f in raw_formats
            if f.get("url") and f.get("ext") != "mhtml"
        ]

        # 2. マニフェスト(HLS/DASH)の厳格な抽出
        manifests = []
        for f in raw_formats:
            f_url = f.get("url", "")
            protocol = f.get("protocol", "")
            # HLS (m3u8) または DASH (mpd) を判定
            if ".m3u8" in f_url or protocol == "m3u8_native":
                manifests.append({"type": "hls", "url": f_url})
            elif ".mpd" in f_url or protocol == "http_dash_segments":
                manifests.append({"type": "dash", "url": f_url})

        response_data = {
            "title": info.get("title"),
            "id": video_id,
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            "formats": formats,
            "_manifests": manifests  # 内部判定用のリスト
        }
        
        # フォーマット数が多い（＝高品質が期待できる）場合は長めにキャッシュ
        dur = 14200 if len(formats) >= 12 else 600
        VIDEO_CACHE[video_id] = (time.time(), response_data, dur)
        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_url(video_id: str):
    """
    HLS (.m3u8) へのリダイレクトを優先し、
    存在しない場合は DASH または Direct URL へフォールバックします。
    """
    data = await get_streams(video_id)
    manifests = data.get("_manifests", [])

    # 1. HLS (.m3u8) を最優先
    hls_target = next((m["url"] for m in manifests if m["type"] == "hls"), None)
    if hls_target:
        return RedirectResponse(url=hls_target)

    # 2. DASH (.mpd) を次点
    dash_target = next((m["url"] for m in manifests if m["type"] == "dash"), None)
    if dash_target:
        return RedirectResponse(url=dash_target)

    # 3. どちらもない場合は、通常のフォーマットから最初のURLを返す
    formats = data.get("formats", [])
    if formats:
        return RedirectResponse(url=formats[0]["url"])

    raise HTTPException(status_code=404, detail="No streamable URL found.")

# --- プレイリスト / チャンネル / ステータス ---

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    cleanup_cache()
    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/watch?list={playlist_id}" if playlist_id.startswith("RD") else f"https://www.youtube.com/playlist?list={playlist_id}"
    
    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl: return ydl.extract_info(url, download=False)
        
    PROCESSING_IDS.add(playlist_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title"), "uploader": e.get("uploader"), "duration": e.get("duration"), "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None} for e in info.get("entries", []) if e]
        res = {"id": playlist_id, "title": info.get("title"), "video_count": len(entries), "entries": entries}
        PLAYLIST_CACHE[playlist_id] = (time.time(), res, 14200)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(playlist_id)

@app.get("/channel/{channel_id}")
async def get_channel(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/{channel_id}/videos" if channel_id.startswith("@") else f"https://www.youtube.com/channel/{channel_id}/videos"
    
    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl: return ydl.extract_info(url, download=False)
        
    PROCESSING_IDS.add(channel_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        videos = [{"id": e.get("id"), "title": e.get("title"), "view_count": e.get("view_count"), "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None} for e in info.get("entries", []) if e]
        res = {"channel_id": info.get("id"), "name": info.get("channel") or info.get("uploader"), "description": info.get("description"), "subscribers": info.get("subscriber_count"), "video_count": len(videos), "videos": videos}
        CHANNEL_CACHE[channel_id] = (time.time(), res, 86400)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(channel_id)

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

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        "videos": {vid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for vid, (ts, _, dur) in VIDEO_CACHE.items()},
        "playlists": {pid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for pid, (ts, _, dur) in PLAYLIST_CACHE.items()},
        "channels": {cid: {"age": int(now - ts), "remaining": int(dur - (now - ts))} for cid, (ts, _, dur) in CHANNEL_CACHE.items()}
    }

@app.delete("/cache/clear")
def clear_cache():
    VIDEO_CACHE.clear()
    PLAYLIST_CACHE.clear()
    CHANNEL_CACHE.clear()
    return {"status": "ok"}
