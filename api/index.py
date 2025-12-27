from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os
import subprocess
import glob
import re
import random
import requests
from typing import List, Optional

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor()
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

# プロキシリストとローテーション
PROXY_LIST: List[str] = []
WORKING_PROXIES: List[str] = []
FAILED_PROXIES: set = set()
last_proxy_fetch = 0
PROXY_FETCH_INTERVAL = 3600  # 1時間ごとに更新

def fetch_free_proxies() -> List[str]:
    """無料プロキシリストを取得"""
    proxies = []
    
    # 複数のソースから取得
    sources = [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt"
    ]
    
    for url in sources:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                lines = response.text.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line and ':' in line and not line.startswith('#'):
                        # http://形式に変換
                        if not line.startswith('http'):
                            line = f'http://{line}'
                        proxies.append(line)
            print(f"Fetched {len(lines)} proxies from {url}")
        except Exception as e:
            print(f"Failed to fetch from {url}: {e}")
    
    # 重複削除
    proxies = list(set(proxies))
    print(f"Total unique proxies collected: {len(proxies)}")
    return proxies

def test_proxy(proxy: str) -> bool:
    """プロキシが動作するかテスト"""
    test_url = "https://www.youtube.com"
    try:
        response = requests.get(
            test_url,
            proxies={"http": proxy, "https": proxy},
            timeout=5
        )
        return response.status_code == 200
    except:
        return False

async def update_proxy_list():
    """プロキシリストを更新"""
    global PROXY_LIST, WORKING_PROXIES, last_proxy_fetch
    
    current_time = time.time()
    if current_time - last_proxy_fetch < PROXY_FETCH_INTERVAL and PROXY_LIST:
        return
    
    print("Fetching new proxy list...")
    loop = asyncio.get_event_loop()
    new_proxies = await loop.run_in_executor(executor, fetch_free_proxies)
    
    if new_proxies:
        PROXY_LIST = new_proxies
        # 最初の10個をテスト
        print("Testing first 10 proxies...")
        working = []
        for proxy in PROXY_LIST[:10]:
            if await loop.run_in_executor(executor, test_proxy, proxy):
                working.append(proxy)
                print(f"✓ Working proxy: {proxy}")
        
        WORKING_PROXIES = working
        last_proxy_fetch = current_time
        print(f"Updated proxy list: {len(PROXY_LIST)} total, {len(WORKING_PROXIES)} tested working")

def get_random_proxy() -> Optional[str]:
    """ランダムにプロキシを選択"""
    # 動作確認済みプロキシを優先
    if WORKING_PROXIES:
        available = [p for p in WORKING_PROXIES if p not in FAILED_PROXIES]
        if available:
            return random.choice(available)
    
    # 未テストのプロキシから選択
    if PROXY_LIST:
        available = [p for p in PROXY_LIST if p not in FAILED_PROXIES]
        if available:
            return random.choice(available)
    
    return None

def get_ydl_opts(use_proxy: bool = True):
    """yt-dlpオプションを取得"""
    opts = {
        "quiet": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "skip_live_postprocessor": True,
        "noplaylist": True,
        "getdescription": False,
        "getduration": False,
        "getcomments": False,
        "socket_timeout": 10,
        "retries": 3
    }
    
    if use_proxy:
        proxy = get_random_proxy()
        if proxy:
            opts["proxy"] = proxy
            print(f"Using proxy: {proxy}")
    
    return opts

CACHE = {}
DEFAULT_CACHE_DURATION = 1800
LONG_CACHE_DURATION = 14400

def cleanup_cache():
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]
    if expired:
        print(f"Cache cleanup: Removed {len(expired)} entries")

async def _fetch_and_cache_info(video_id: str, max_retries: int = 5):
    current_time = time.time()
    cleanup_cache()
    
    # キャッシュチェック
    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            return data
    
    # プロキシリスト更新
    await update_proxy_list()
    
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # 複数のプロキシで試行
    for attempt in range(max_retries):
        ydl_opts = get_ydl_opts(use_proxy=len(PROXY_LIST) > 0)
        current_proxy = ydl_opts.get("proxy")
        
        def fetch_info():
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        
        try:
            loop = asyncio.get_event_loop()
            raw_info = await loop.run_in_executor(executor, fetch_info)
            
            formats = [
                {
                    "itag": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution"),
                    "fps": f.get("fps"),
                    "acodec": f.get("acodec"),
                    "vcodec": f.get("vcodec"),
                    "url": f.get("url"),
                    "protocol": f.get("protocol"),
                    "vbr": f.get("vbr"),
                    "abr": f.get("abr"),
                }
                for f in raw_info.get("formats", [])
                if f.get("url") and f.get("ext") != "mhtml"
            ]
            
            response_data = {
                "title": raw_info.get("title"),
                "id": video_id,
                "formats": formats
            }
            
            cache_duration = (
                LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
            )
            CACHE[video_id] = (current_time, response_data, cache_duration)
            print(f"✓ Cached {video_id} for {cache_duration}s. Formats: {len(formats)}")
            
            return response_data
            
        except Exception as e:
            error_msg = str(e)
            print(f"Attempt {attempt + 1}/{max_retries} failed: {error_msg}")
            
            # プロキシが原因の場合、失敗リストに追加
            if current_proxy and ("proxy" in error_msg.lower() or "tunnel" in error_msg.lower()):
                FAILED_PROXIES.add(current_proxy)
                print(f"✗ Marked proxy as failed: {current_proxy}")
            
            # 最後の試行でない場合は続行
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            
            # 全て失敗した場合
            raise HTTPException(
                status_code=500,
                detail=f"Failed after {max_retries} attempts: {error_msg}"
            )

@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    return await _fetch_and_cache_info(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    try:
        info_data = await _fetch_and_cache_info(video_id)
    except HTTPException as e:
        raise e
    
    m3u8_formats = [
        f for f in info_data["formats"]
        if f.get("url") and (
            ".m3u8" in f["url"] or 
            f.get("ext") == "m3u8" or 
            f.get("protocol") in ["m3u8_native", "http_dash_segments"]
        )
    ]
    
    if not m3u8_formats:
        raise HTTPException(status_code=404, detail="No m3u8 streams found")
    
    return {
        "title": info_data["title"],
        "id": video_id,
        "m3u8_formats": m3u8_formats
    }

@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):
    try:
        info_data = await _fetch_and_cache_info(video_id)
    except HTTPException as e:
        raise e
    
    formats = info_data["formats"]
    
    best_video = next(
        (f for f in sorted(formats, key=lambda x: x.get("vbr") or 0, reverse=True)
         if f.get("vcodec") not in ["none", None] and f.get("acodec") in ["none", None]),
        None
    )
    
    best_audio = next(
        (f for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True)
         if f.get("acodec") not in ["none", None] and f.get("vcodec") in ["none", None]),
        None
    )
    
    if not best_video and not best_audio:
        raise HTTPException(status_code=404, detail="No suitable streams found")
    
    return {
        "title": info_data["title"],
        "id": video_id,
        "best_video": best_video,
        "best_audio": best_audio,
        "note": "Combine streams using FFmpeg for best quality"
    }

@app.get("/proxy/stats")
def proxy_stats():
    """プロキシ統計情報"""
    return {
        "total_proxies": len(PROXY_LIST),
        "working_proxies": len(WORKING_PROXIES),
        "failed_proxies": len(FAILED_PROXIES),
        "cache_entries": len(CACHE)
    }

@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    if video_id in CACHE:
        del CACHE[video_id]
        return {"status": "success", "message": f"Cache deleted for {video_id}"}
    raise HTTPException(status_code=404, detail="Cache entry not found")

@app.get("/cache")
def list_cache():
    now = time.time()
    cleanup_cache()
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
