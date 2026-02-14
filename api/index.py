from fastapi import FastAPI, HTTPException, Query
from yt_dlp import YoutubeDL
import time
import asyncio
import httpx
import re
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=20)

# 字幕メタデータ取得用
ydl_opts_subs = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "writesubtitles": True,
    "writeautomaticsub": True,
    "subtitleslangs": [".*"],
}

# --- 修正ポイント：動画URL取得用 (all_formatsをTrueにして全てのストリームを取得) ---
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "extract_flat": False,
}

VIDEO_CACHE = {}      
SUBTITLE_LIST_CACHE = {} 
SUBTITLE_CONTENT_CACHE = {} 
LONG_CACHE_DURATION = 14200     

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, SUBTITLE_LIST_CACHE, SUBTITLE_CONTENT_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

def parse_vtt(vtt_text: str):
    lines = vtt_text.strip().split('\n')
    results = []
    time_pattern = re.compile(r'(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})')
    
    current_entry = None
    for line in lines:
        match = time_pattern.match(line)
        if match:
            if current_entry: results.append(current_entry)
            current_entry = {"start": match.group(1), "end": match.group(2), "text": ""}
        elif current_entry and line.strip() and not any(s in line for s in ['WEBVTT', 'Kind:', 'Language:']):
            clean_text = re.sub(r'<[^>]+>', '', line).strip()
            if clean_text:
                current_entry["text"] += clean_text + " "
                
    if current_entry: results.append(current_entry)
    for item in results: item["text"] = item["text"].strip()
    return [r for r in results if r["text"]]

@app.get("/subtitles/{video_id}")
async def get_subtitle_list(video_id: str):
    cleanup_cache()
    if video_id in SUBTITLE_LIST_CACHE:
        ts, data, dur = SUBTITLE_LIST_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        def fetch():
            with YoutubeDL(ydl_opts_subs) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        available_languages = []
        all_langs = set(list(manual.keys()) + list(auto.keys()))
        
        for lang in all_langs:
            formats = manual.get(lang) or auto.get(lang)
            has_vtt = any(s.get("ext") == "vtt" for s in formats)
            if has_vtt:
                available_languages.append({
                    "lang": lang,
                    "name": (manual.get(lang) or auto.get(lang))[0].get("name", lang),
                    "is_auto": lang in auto and lang not in manual
                })

        res = {"video_id": video_id, "total_languages": len(available_languages), "languages": available_languages}
        SUBTITLE_LIST_CACHE[video_id] = (time.time(), res, LONG_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/subtitles/{video_id}/content")
async def get_subtitle_content(video_id: str, lang: str = "ja"):
    cleanup_cache()
    cache_key = f"{video_id}_{lang}"
    if cache_key in SUBTITLE_CONTENT_CACHE:
        ts, data, dur = SUBTITLE_CONTENT_CACHE[cache_key]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        def fetch():
            with YoutubeDL(ydl_opts_subs) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        formats = (info.get("subtitles") or {}).get(lang) or (info.get("automatic_captions") or {}).get(lang)
        if not formats:
            raise HTTPException(status_code=404, detail="Language not found")
        
        target_url = next((s["url"] for s in formats if s["ext"] == "vtt"), None)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(target_url)
            parsed_data = parse_vtt(resp.text)
            res = {"video_id": video_id, "lang": lang, "segments": parsed_data}
            SUBTITLE_CONTENT_CACHE[cache_key] = (time.time(), res, LONG_CACHE_DURATION)
            return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 修正ポイント：JSONレスポンス形式をアップロードされたデータに合わせる ---
@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    cleanup_cache()
    if video_id in VIDEO_CACHE:
        ts, data, dur = VIDEO_CACHE[video_id]
        if time.time() - ts < dur: return data

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        def fetch():
            with YoutubeDL(ydl_opts_base) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        # 抽出するフォーマット情報をアップロードされたJSONの構造に合わせる
        formats = []
        for f in info.get("formats", []):
            if not f.get("url"): continue
            
            formats.append({
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "url": f.get("url"),
                "mime": f.get("vcodec") if f.get("vcodec") != "none" else f.get("acodec"), # 代用的な識別
                "clen": f.get("filesize") or f.get("filesize_approx"),
                "dur": info.get("duration"),
                "width": f.get("width"),
                "height": f.get("height"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec")
            })
            
        res = {
            "title": info.get("title"),
            "id": video_id,
            "formats": formats
        }
        
        VIDEO_CACHE[video_id] = (time.time(), res, LONG_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
