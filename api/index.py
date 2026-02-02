from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os

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

# クッキーファイルのパス（apiディレクトリ内にあることを想定）
# ファイル名はアップロードされた名前に合わせる
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "youtube-cookies.txt")

# 基本的な yt-dlp オプション
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    "cookiefile": COOKIE_FILE,
    # --- 読み取り専用エラー(Errno 30)対策 ---
    "no_cookies_file": True,  # クッキーの更新をファイルに書き戻さない
}

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
    # ファイル存在チェック
    if not os.path.exists(COOKIE_FILE):
        raise HTTPException(status_code=500, detail=f"Cookie file not found at {COOKIE_FILE}")

    current_time = time.time()
    cleanup_cache()

    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        # クッキーを使用して情報を取得
        with YoutubeDL(ydl_opts_base) as ydl:
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

        # 取得できたフォーマット数に応じてキャッシュ時間を調整
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
        "cookie_file_exists": os.path.exists(COOKIE_FILE)
    }

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        vid: {"remaining_sec": int(dur - (now - ts))}
        for vid, (ts, _, dur) in CACHE.items()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
