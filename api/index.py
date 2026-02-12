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

ydl_opts_base = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "best",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "extract_flat": False,
}

ydl_opts_flat = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "lazy_playlist": True,
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007"
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
        
        formats = [{
            "itag": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "url": f.get("url")
        } for f in info.get("formats", []) if f.get("url") and f.get("ext") != "mhtml"]

        res = {"title": info.get("title"), "id": video_id, "formats": formats}
        dur = LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        VIDEO_CACHE[video_id] = (time.time(), res, dur)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/m3u8/{video_id}")
async def get_m3u8(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    PROCESSING_IDS.add(video_id)
    try:
        def fetch():
            opts = {**ydl_opts_base, "user_agent": "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        streams = [{"url": f.get("url"), "resolution": f.get("resolution"), "protocol": f.get("protocol"), "ext": f.get("ext")}
                   for f in info.get("formats", []) if f.get("protocol") == "m3u8_native" or ".m3u8" in f.get("url", "")]

        if not streams and info.get("hls_url"):
            streams.append({"url": info.get("hls_url"), "resolution": "adaptive", "protocol": "m3u8_native", "ext": "m3u8"})

        return {"title": info.get("title"), "video_id": video_id, "m3u8_streams": streams}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(video_id)

@app.get("/channel/home/{channel_id}")
async def get_channel_home(channel_id: str):
    cleanup_cache()
    cache_key = f"home_{channel_id}"
    if cache_key in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[cache_key]
        if time.time() - ts < dur: return data

    if channel_id.startswith("UC"):
        url = f"https://www.youtube.com/channel/{channel_id}"
    elif channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}"
    
    PROCESSING_IDS.add(cache_key)
    try:
        def fetch():
            opts = {
                **ydl_opts_base,
                "extract_flat": True,
                "playlist_items": "1",
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        sub_count = info.get("channel_follower_count") or info.get("subscriber_count")
        
        featured_video = None
        entries = info.get("entries", [])
        if entries and len(entries) > 0:
            featured_video = entries[0].get("id")

        res = {
            "id": info.get("id") or channel_id,
            "name": info.get("channel") or info.get("uploader") or info.get("title"),
            "subscriber_count": sub_count,
            "description": info.get("description"),
            "avatar": get_best_thumbnail(info.get("thumbnails")),
            "banner": info.get("thumbnails")[-1].get("url") if info.get("thumbnails") else None,
            "featured_video": featured_video
        }
        
        CHANNEL_CACHE[cache_key] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch home: {str(e)}")
    finally:
        PROCESSING_IDS.discard(cache_key)

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
    cache_key = f"{playlist_id}_{v}" if v else playlist_id
    if cache_key in PLAYLIST_CACHE:
        ts, data, dur = PLAYLIST_CACHE[cache_key]
        if time.time() - ts < dur: return data
    
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    if playlist_id.startswith("RD"):
        url = f"https://www.youtube.com/watch?v={v}&list={playlist_id}" if v else f"https://www.youtube.com/watch?list={playlist_id}"
    
    PROCESSING_IDS.add(playlist_id)
    try:
        def fetch():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        entries = [{"id": e.get("id"), "title": e.get("title"), "thumbnail": get_best_thumbnail(e.get("thumbnails"))}
                   for e in info.get("entries", []) if e]
        res = {"id": playlist_id, "title": info.get("title"), "entries": entries}
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
    else:
        url = f"https://www.youtube.com/{channel_id}/shorts"
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
