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

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007"
}

# キャッシュと処理中リスト
CACHE = {}
PROCESSING_IDS = set()  # 現在処理中の video_id を保持
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

    # --- 処理中管理の追加 ---
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 成功・失敗に関わらず、終わったら処理中リストから削除
        if video_id in PROCESSING_IDS:
            PROCESSING_IDS.remove(video_id)

# --- 処理状況確認用API ---
@app.get("/status")
def get_status():
    """現在処理中のIDとキャッシュされているIDのサマリーを返す"""
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
    else:
        raise HTTPException(status_code=404, detail="指定されたIDのキャッシュは存在しません。")

@app.get("/cache")
def list_cache():
    now = time.time()
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur,
            "is_processing": vid in PROCESSING_IDS  # 個別のキャッシュ情報にも処理中かを入れる
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
