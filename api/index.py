from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# スレッドプールを少し多めに確保して並列性を上げる
executor = ThreadPoolExecutor(max_workers=20)

# --- 超高速用共通設定 ---
ydl_opts_base = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "socket_timeout": 5,        # タイムアウトを短く
    "retries": 1,               # リトライを減らす
    "extract_flat": "in_playlist",
    "dynamic_mpd": False,       # 不要な動的生成をオフ
    "youtube_include_dash_manifest": False, # 重いマニフェストを無視
    "youtube_include_hls_manifest": False,
}

# 動画URL取得用（必要最低限のデータに絞る）
ydl_opts_video = {
    **ydl_opts_base,
    "extract_flat": False,
    "skip_download": True,
    "format": "best", # 複雑なフォーマット結合を避けて単一の最適URLを狙う
}

# プレイリスト・チャンネル用（最速設定）
ydl_opts_flat = {
    **ydl_opts_base,
    "playlist_items": "1-50",
    "lazy_playlist": True, # 読み込みながら処理
}

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
        # 動画取得時に余計な情報を取らないよう内部フラグを最適化
        with YoutubeDL({**ydl_opts_video, "noplaylist": True}) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(video_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        # 必要なものだけ抽出（ループを最小限に）
        res = {
            "title": info.get("title"),
            "url": info.get("url"), # 直接再生可能なURL
            "formats": [{"url": f.get("url"), "ext": f.get("ext"), "res": f.get("resolution")} for f in info.get("formats", []) if f.get("url")][:10]
        }
        VIDEO_CACHE[video_id] = (time.time(), res, 600)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(video_id)

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    cleanup_cache()
    if playlist_id in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[playlist_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?list={playlist_id}" if playlist_id.startswith("RD") else f"https://www.youtube.com/playlist?list={playlist_id}"
    
    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(playlist_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title")} for e in info.get("entries", []) if e]
        res = {"title": info.get("title"), "entries": entries}
        PLAYLIST_CACHE[playlist_id] = (time.time(), res, 3600)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(playlist_id)

@app.get("/channel/{channel_id}")
async def get_channel(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data

    # チャンネルURLの末尾に /videos をつけるのが最速（ホームよりパースが楽）
    base_url = f"https://www.youtube.com/{channel_id}" if channel_id.startswith("@") else f"https://www.youtube.com/channel/{channel_id}"
    url = f"{base_url}/videos"

    def fetch():
        with YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(channel_id)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        videos = [{"id": e.get("id"), "title": e.get("title"), "thumb": e.get("thumbnails", [{}])[-1].get("url")} for e in info.get("entries", []) if e]
        res = {"name": info.get("uploader"), "videos": videos}
        CHANNEL_CACHE[channel_id] = (time.time(), res, 86400)
        return res
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: PROCESSING_IDS.discard(channel_id)
