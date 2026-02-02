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

# --- クッキーファイルとオプションの設定 ---
# 1. アップロードされた「youtube-cookies.txt」をこのスクリプトと同じ階層に置いてください。
COOKIE_FILE = "youtube-cookies.txt"

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    "cookiefile": COOKIE_FILE,  # <-- ここでクッキーを指定
    # "proxy": "http://..."      # プロキシは不要なら削除またはコメントアウト
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
    current_time = time.time()
    cleanup_cache()

    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    PROCESSING_IDS.add(video_id)
    try:
        loop = asyncio.get_event_loop()
        # クッキーを使用して情報を取得
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
        # クッキーが期限切れの場合、ここでエラーが出ることが多いです
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")
    finally:
        if video_id in PROCESSING_IDS:
            PROCESSING_IDS.remove(video_id)

# --- その他のエンドポイントは変更なし ---
@app.get("/status")
def get_status():
    return {
        "processing_count": len(PROCESSING_IDS),
        "processing_ids": list(PROCESSING_IDS),
        "cache_count": len(CACHE)
    }

@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    if video_id in CACHE:
        del CACHE[video_id]
        return {"status": "success", "message": f"{video_id} のキャッシュを削除しました。"}
    raise HTTPException(status_code=404, detail="Cache not found")

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "is_processing": vid in PROCESSING_IDS
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
