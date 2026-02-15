from fastapi import FastAPI, HTTPException, Query
from yt_dlp import YoutubeDL
import time
import asyncio
import re
import httpx
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 25件を同時に処理するためにスレッド数を調整
executor = ThreadPoolExecutor(max_workers=30)

# --- yt-dlp オプション設定 ---

# 安定性を重視したベースオプション
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "ignore_no_formats_error": True,
    "ignoreerrors": True,
    "no_warnings": True,
    "extract_flat": False,
}

ydl_opts_flat = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "ignore_no_formats_error": True
}

# 字幕取得用オプション
ydl_opts_subs = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "writesubtitles": True,
    "writeautomaticsub": True,
    "subtitleslangs": [".*"],
}

# --- キャッシュ & 状態管理 ---
VIDEO_CACHE = {}      
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
STREAMS_CACHE = {}
SUBTITLE_LIST_CACHE = {} 
SUBTITLE_CONTENT_CACHE = {} 
PROCESSING_IDS = set()

LONG_CACHE_DURATION = 14200
CHANNEL_CACHE_DURATION = 7200

# --- ユーティリティ ---

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

def get_best_thumbnail(thumbnails):
    if not thumbnails: return None
    return thumbnails[-1].get("url")

def parse_vtt(vtt_text: str):
    """VTT形式を解析して扱いやすいJSONリストに変換"""
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

# --- API ルート ---

@app.get("/status")
def get_status():
    return {
        "status": "operational",
        "processing_count": len(PROCESSING_IDS),
        "cache_stats": {
            "videos": len(VIDEO_CACHE),
            "playlists": len(PLAYLIST_CACHE),
            "channels": len(CHANNEL_CACHE),
            "streams": len(STREAMS_CACHE)
        }
    }

@app.get("/cache")
def get_cache_info():
    return {
        "video": list(VIDEO_CACHE.keys()),
        "playlist": list(PLAYLIST_CACHE.keys()),
        "channel": list(CHANNEL_CACHE.keys()),
        "streams": list(STREAMS_CACHE.keys())
    }

@app.delete("/cache")
def clear_cache():
    for c in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE]: c.clear()
    return {"message": "Caches cleared"}

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
        if not info: raise Exception("Info fetch failed")
        
        formats = [{"itag": f.get("format_id"), "ext": f.get("ext"), "resolution": f.get("resolution"), "url": f.get("url")} 
                   for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]
        res = {"title": info.get("title"), "id": video_id, "formats": formats}
        VIDEO_CACHE[video_id] = (time.time(), res, 600)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str, v: Optional[str] = Query(None)):
    cleanup_cache()
    cache_key = f"pl_{playlist_id}_{v}" if v else f"pl_{playlist_id}"
    
    if cache_key in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[cache_key]
        if time.time() - ts < dur: return data
    
    if playlist_id.startswith("RD"):
        url = f"https://www.youtube.com/watch?v={v}&list={playlist_id}" if v else f"https://www.youtube.com/watch?list={playlist_id}"
    else:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
    
    PROCESSING_IDS.add(playlist_id)
    try:
        def fetch():
            opts = {**ydl_opts_flat, "playlist_items": "1-50"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        if not info: raise Exception("No playlist info")

        entries = []
        for e in info.get("entries", []):
            if not e: continue
            entries.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
            })

        res = {
            "title": info.get("title") or "Playlist",
            "video_count": info.get("playlist_count") or len(entries),
            "entries": entries
        }
        PLAYLIST_CACHE[cache_key] = (time.time(), res, LONG_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(playlist_id)

@app.get("/channel/{channel_id}")
async def get_channel_videos(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data
    
    # URLの組み立て
    if channel_id.startswith("UC"):
        base_url = f"https://www.youtube.com/channel/{channel_id}"
    elif channel_id.startswith("@"):
        base_url = f"https://www.youtube.com/{channel_id}"
    else:
        base_url = f"https://www.youtube.com/channel/{channel_id}"
        
    videos_url = f"{base_url}/videos"
    
    PROCESSING_IDS.add(channel_id)
    try:
        def fetch_data():
            # 1. チャンネルの基本情報（アイコン、登録者数、名前）を取得
            with YoutubeDL(ydl_opts_base) as ydl:
                # process=Trueにしてメタデータをしっかり取得
                meta = ydl.extract_info(base_url, download=False, process=True)
            
            # 2. 動画一覧を取得
            with YoutubeDL(ydl_opts_flat) as ydl:
                try:
                    video_info = ydl.extract_info(videos_url, download=False)
                except:
                    # /videosが失敗した場合はトップページから
                    video_info = ydl.extract_info(base_url, download=False)
            return meta, video_info
        
        loop = asyncio.get_event_loop()
        meta_info, video_info = await loop.run_in_executor(executor, fetch_data)
        
        # アイコンURLの取得
        icon_url = get_best_thumbnail(meta_info.get("thumbnails"))
        
        # 登録者数の取得（フロントエンドの formatCount に渡す数値）
        sub_count = meta_info.get("channel_follower_count") or meta_info.get("subscriber_count")
        
        # フロントエンドの変数名に合わせたレスポンス構造
        res = {
            "channel_id": meta_info.get("id") or channel_id,
            "name": meta_info.get("channel") or meta_info.get("uploader") or meta_info.get("title"),
            "icon": icon_url,          # フロントエンドの img.src 用
            "avatar": icon_url,        # 予備
            "description": meta_info.get("description"),
            "subscriber_count": sub_count,
            "videos": [
                {
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "view_count": e.get("view_count"), 
                    "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                    "duration": e.get("duration")
                }
                for e in video_info.get("entries", []) if e and e.get("id")
            ]
        }
        
        CHANNEL_CACHE[channel_id] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except Exception as e:
        print(f"Error fetching channel: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)

@app.get("/channel/stream/{channel_id}")
async def get_channel_streams(channel_id: str):
    cleanup_cache()
    if channel_id.startswith("UC"):
        url = f"https://www.youtube.com/channel/{channel_id}/streams"
    elif channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/streams"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/streams"
    
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        entries = []
        for e in info.get("entries", []):
            if not e: continue
            entries.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "duration": e.get("duration"),
                "view_count": e.get("view_count")
            })
        
        return {"channel": channel_id, "streams": entries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/short/{channel_id}")
async def get_shorts(channel_id: str):
    cleanup_cache()
    if channel_id.startswith("UC"):
        url = f"https://www.youtube.com/channel/{channel_id}/shorts"
    elif channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/shorts"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/shorts"
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        shorts = [{"id": e.get("id"), "title": e.get("title"), "thumbnail": get_best_thumbnail(e.get("thumbnails"))}
                  for e in info.get("entries", []) if e]
        return {"channel": channel_id, "shorts": shorts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
            if any(s.get("ext") == "vtt" for s in formats):
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
        if not formats: raise HTTPException(status_code=404, detail="Language not found")
        
        target_url = next((s["url"] for s in formats if s["ext"] == "vtt"), None)
        async with httpx.AsyncClient() as client:
            resp = await client.get(target_url)
            parsed_data = parse_vtt(resp.text)
            res = {"video_id": video_id, "lang": lang, "segments": parsed_data}
            SUBTITLE_CONTENT_CACHE[cache_key] = (time.time(), res, LONG_CACHE_DURATION)
            return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
