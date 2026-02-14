from fastapi import FastAPI, HTTPException, Query
from yt_dlp import YoutubeDL
import time
import asyncio
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

executor = ThreadPoolExecutor(max_workers=20)

# 基本設定（字幕取得オプションを追加）
ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "extract_flat": False,
    "ignore_no_formats_error": True,
    "ignoreerrors": True,
    "writesubtitles": True,              # 手動字幕を取得
    "writeautomaticsub": True,          # 自動生成字幕を取得
    "subtitleslangs": ["ja", "en", ".*"], # 日本語、英語を優先し、それ以外も取得可能にする
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

VIDEO_CACHE = {}      
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
PROCESSING_IDS = set()

DEFAULT_CACHE_DURATION = 600    
LONG_CACHE_DURATION = 14200     
CHANNEL_CACHE_DURATION = 86400  

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE]:
        expired = [k for k, (ts, _, dur) in cache.items() if now - ts >= dur]
        for k in expired:
            del cache[k]

def get_best_thumbnail(thumbnails):
    if not thumbnails: return None
    return thumbnails[-1].get("url")

@app.get("/status")
def get_status():
    return {
        "processing_count": len(PROCESSING_IDS),
        "processing_ids": list(PROCESSING_IDS)
    }

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
            with YoutubeDL(ydl_opts_base) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        if not info: raise Exception("No info found")

        # 動画フォーマット
        formats = [{
            "itag": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "url": f.get("url")
        } for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]

        # --- 字幕データの抽出 ---
        subtitles = []
        manual_subs = info.get("subtitles") or {}
        auto_subs = info.get("automatic_captions") or {}
        
        # 全ての字幕ソースをマージ
        all_langs = set(list(manual_subs.keys()) + list(auto_subs.keys()))
        
        for lang in all_langs:
            # 手動字幕があれば優先、なければ自動生成を使用
            formats_list = manual_subs.get(lang) or auto_subs.get(lang)
            if not formats_list: continue
            
            # ブラウザで再生しやすい vtt 形式のURLを抽出
            vtt_url = next((s.get("url") for s in formats_list if s.get("ext") == "vtt"), None)
            
            if vtt_url:
                subtitles.append({
                    "lang": lang,
                    "url": vtt_url,
                    "is_auto": lang in auto_subs and lang not in manual_subs
                })

        res = {
            "title": info.get("title"), 
            "id": video_id, 
            "formats": formats,
            "subtitles": subtitles
        }
        
        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (time.time(), res, dur)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/channel/streams/{channel_id}")
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
            opts = {
                **ydl_opts_base,
                "extract_flat": False,
                "playlist_items": "1-50",
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        streams = []
        entries = info.get("entries", []) if info else []
        
        for e in entries:
            if not e: continue
            status = e.get("live_status")
            is_live = status == "live"
            is_upcoming = status == "upcoming" or e.get("availability") == "upcoming"
            viewers = e.get("concurrent_view_count") or e.get("waiting_count") or 0
            
            streams.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "status": "live" if is_live else "upcoming" if is_upcoming else "archived",
                "viewers": int(viewers),
                "scheduled_start": e.get("release_timestamp") or e.get("timestamp"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "is_live": is_live,
                "is_upcoming": is_upcoming
            })

        return {
            "channel": info.get("uploader") or info.get("title") if info else channel_id,
            "channel_id": info.get("id") if info else None,
            "streams": streams
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch: {str(e)}")

@app.get("/channel/{channel_id}")
async def get_channel_videos(channel_id: str):
    cleanup_cache()
    if channel_id in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[channel_id]
        if time.time() - ts < dur: return data
    
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
            with YoutubeDL(ydl_opts_base) as ydl:
                meta = ydl.extract_info(base_url, download=False, process=False)
            with YoutubeDL(ydl_opts_flat) as ydl:
                try:
                    videos = ydl.extract_info(videos_url, download=False)
                except:
                    videos = ydl.extract_info(base_url, download=False)
            return meta, videos
        
        loop = asyncio.get_event_loop()
        meta_info, video_info = await loop.run_in_executor(executor, fetch_data)
        
        sub_count = meta_info.get("channel_follower_count") or meta_info.get("subscriber_count")
        
        res = {
            "channel_id": meta_info.get("id") or video_info.get("id"),
            "name": meta_info.get("channel") or meta_info.get("uploader"),
            "description": meta_info.get("description"),
            "subscriber_count": sub_count,
            "avatar": get_best_thumbnail(meta_info.get("thumbnails")),
            "videos": [{"id": e.get("id"), "title": e.get("title"), "view_count": e.get("view_count"), 
                        "thumbnail": get_best_thumbnail(e.get("thumbnails")), "duration": e.get("duration")}
                       for e in video_info.get("entries", []) if e and e.get("id")]
        }
        
        CHANNEL_CACHE[channel_id] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)
        
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
            opts = { **ydl_opts_flat, "playlist_items": "1-50" }
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
