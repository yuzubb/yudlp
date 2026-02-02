from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
import tempfile

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

# 元のクッキーファイルのパス
ORIGINAL_COOKIE = os.path.join(os.path.dirname(__file__), "youtube-cookies.txt")

# キャッシュと処理中リスト
CACHE = {}
PROCESSING_IDS = set()
DEFAULT_CACHE_DURATION = 600
LONG_CACHE_DURATION = 14200

def cleanup_cache():
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    # クッキーファイルの存在確認
    if not os.path.exists(ORIGINAL_COOKIE):
        raise HTTPException(status_code=500, detail="Cookie file not found in API directory.")

    current_time = time.time()
    cleanup_cache()

    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        # --- 読み取り専用エラー対策: /tmp に一時的なコピーを作成 ---
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp:
            with open(ORIGINAL_COOKIE, 'r') as f:
                tmp.write(f.read())
            temp_cookie_path = tmp.name

        try:
            ydl_opts = {
                "quiet": True,
                "skip_download": True,
                "nocheckcertificate": True,
                "format": "bestvideo+bestaudio/best",
                "cookiefile": temp_cookie_path,
                "no_cookies_file": True, # 書き込みを行わない設定
            }
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        finally:
            # 使用後に一時ファイルを削除
            if os.path.exists(temp_cookie_path):
                os.remove(temp_cookie_path)

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
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")
    finally:
        if video_id in PROCESSING_IDS:
            PROCESSING_IDS.remove(video_id)

@app.get("/status")
def get_status():
    return {
        "processing_count": len(PROCESSING_IDS),
        "cache_count": len(CACHE),
        "cookie_path": ORIGINAL_COOKIE,
        "cookie_exists": os.path.exists(ORIGINAL_COOKIE)
    }
