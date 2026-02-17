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

# ストリーム取得用オプション（配信中の視聴者数を取得）
ydl_opts_streams = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007",
    "ignore_no_formats_error": True,
    "ignoreerrors": True,
    "no_warnings": True,
}

# --- キャッシュ & 状態管理 ---
VIDEO_CACHE = {}      
PLAYLIST_CACHE = {}
CHANNEL_CACHE = {}
STREAMS_CACHE = {}
CHANNEL_STREAMS_CACHE = {}
CHANNEL_FEATURED_CACHE = {}
SUBTITLE_LIST_CACHE = {} 
SUBTITLE_CONTENT_CACHE = {} 
PROCESSING_IDS = set()

LONG_CACHE_DURATION = 14200
CHANNEL_CACHE_DURATION = 7200

# --- ユーティリティ ---

def cleanup_cache():
    now = time.time()
    for cache in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE, CHANNEL_STREAMS_CACHE, CHANNEL_FEATURED_CACHE]:
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
            "streams": len(STREAMS_CACHE),
            "channel_streams": len(CHANNEL_STREAMS_CACHE),
            "channel_featured": len(CHANNEL_FEATURED_CACHE)
        }
    }

@app.get("/cache")
def get_cache_info():
    return {
        "video": list(VIDEO_CACHE.keys()),
        "playlist": list(PLAYLIST_CACHE.keys()),
        "channel": list(CHANNEL_CACHE.keys()),
        "streams": list(STREAMS_CACHE.keys()),
        "channel_streams": list(CHANNEL_STREAMS_CACHE.keys()),
        "channel_featured": list(CHANNEL_FEATURED_CACHE.keys())
    }

@app.delete("/cache")
def clear_cache():
    for c in [VIDEO_CACHE, PLAYLIST_CACHE, CHANNEL_CACHE, STREAMS_CACHE, CHANNEL_STREAMS_CACHE, CHANNEL_FEATURED_CACHE]:
        c.clear()
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
        videos_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    elif channel_id.startswith("@"):
        videos_url = f"https://www.youtube.com/{channel_id}/videos"
    else:
        videos_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    
    PROCESSING_IDS.add(channel_id)
    try:
        def fetch_data():
            # 1回の呼び出しで全情報を取得
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(videos_url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_data)
        
        if not info:
            raise HTTPException(status_code=404, detail="Channel not found")
        
        # アイコンURLの取得
        icon_url = get_best_thumbnail(info.get("thumbnails"))
        
        # 登録者数の取得
        sub_count = info.get("channel_follower_count") or info.get("subscriber_count")
        
        # チャンネル名の取得
        channel_name = info.get("channel") or info.get("uploader") or info.get("title")
        
        # フロントエンドの変数名に合わせたレスポンス構造
        res = {
            "channel_id": info.get("channel_id") or info.get("id") or channel_id,
            "name": channel_name,
            "icon": icon_url,
            "avatar": icon_url,
            "description": info.get("description"),
            "subscriber_count": sub_count,
            "videos": [
                {
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "view_count": e.get("view_count"), 
                    "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                    "duration": e.get("duration")
                }
                for e in info.get("entries", []) if e and e.get("id")
            ]
        }
        
        CHANNEL_CACHE[channel_id] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching channel: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(channel_id)

@app.get("/channel/feature/{channel_id}")
async def get_channel_featured(channel_id: str):
    """チャンネルのfeaturedページから情報を取得"""
    cleanup_cache()
    
    # @付きIDに変換
    if not channel_id.startswith("@") and not channel_id.startswith("UC"):
        channel_id = f"@{channel_id}"
    
    cache_key = f"feat_{channel_id}"
    if cache_key in CHANNEL_CACHE:
        ts, data, dur = CHANNEL_CACHE[cache_key]
        if time.time() - ts < dur:
            return data
    
    # URLの組み立て
    if channel_id.startswith("UC"):
        featured_url = f"https://www.youtube.com/channel/{channel_id}/featured"
    elif channel_id.startswith("@"):
        featured_url = f"https://www.youtube.com/{channel_id}/featured"
    else:
        featured_url = f"https://www.youtube.com/channel/{channel_id}/featured"
    
    PROCESSING_IDS.add(cache_key)
    try:
        def fetch_data():
            with YoutubeDL(ydl_opts_flat) as ydl:
                return ydl.extract_info(featured_url, download=False)
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_data)
        
        if not info:
            raise HTTPException(status_code=404, detail="Channel featured page not found")
        
        # チャンネル基本情報
        icon_url = get_best_thumbnail(info.get("thumbnails"))
        sub_count = info.get("channel_follower_count") or info.get("subscriber_count")
        channel_name = info.get("channel") or info.get("uploader") or info.get("title")
        
        # featuredページの動画を取得
        featured_videos = []
        for e in info.get("entries", [])[:12]:  # 最大12件
            if not e or not e.get("id"):
                continue
            featured_videos.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "view_count": e.get("view_count"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "duration": e.get("duration"),
                "published": e.get("upload_date")
            })
        
        res = {
            "channel_id": info.get("channel_id") or info.get("id") or channel_id,
            "name": channel_name,
            "icon": icon_url,
            "description": info.get("description"),
            "subscriber_count": sub_count,
            "featured_videos": featured_videos,
            "video_count": len(featured_videos)
        }
        
        CHANNEL_CACHE[cache_key] = (time.time(), res, CHANNEL_CACHE_DURATION)
        return res
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching featured page: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(cache_key)

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
            with YoutubeDL(ydl_opts_streams) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch)
        
        if not info:
            return {"channel": channel_id, "streams": []}
        
        entries = []
        for e in info.get("entries", []):
            if not e: continue
            
            video_id = e.get("id")
            
            # view_countがnullの場合は配信中または配信予定の可能性があるので詳細取得
            if e.get("view_count") is None and video_id:
                def fetch_detail():
                    with YoutubeDL(ydl_opts_base) as ydl:
                        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                
                try:
                    detail = await loop.run_in_executor(executor, fetch_detail)
                    is_live = detail.get("is_live", False)
                    view_count = detail.get("concurrent_view_count") if is_live else detail.get("view_count", 0)
                    duration = detail.get("duration")
                    
                    entries.append({
                        "id": video_id,
                        "title": e.get("title"),
                        "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                        "duration": duration,
                        "view_count": view_count if view_count is not None else 0,
                        "is_live": is_live
                    })
                except Exception as detail_error:
                    print(f"Detail fetch error for {video_id}: {detail_error}")
                    # 詳細取得失敗時はデフォルト値
                    entries.append({
                        "id": video_id,
                        "title": e.get("title"),
                        "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                        "duration": e.get("duration"),
                        "view_count": 0,
                        "is_live": False
                    })
            else:
                # view_countがある場合は通常通り
                is_live = e.get("is_live", False)
                view_count = e.get("view_count", 0)
                
                entries.append({
                    "id": video_id,
                    "title": e.get("title"),
                    "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                    "duration": e.get("duration"),
                    "view_count": view_count if view_count is not None else 0,
                    "is_live": is_live
                })
        
        return {"channel": channel_id, "streams": entries}
    except Exception as e:
        print(f"Stream fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/channel/feature/{channel_id}")
async def get_channel_featured(channel_id: str):
    """チャンネルのfeaturedページから情報を取得"""
    cleanup_cache()
    
    if channel_id in CHANNEL_FEATURED_CACHE:
        ts, data, dur = CHANNEL_FEATURED_CACHE[channel_id]
        if time.time() - ts < dur:
            return data
    
    # URL組み立て
    if channel_id.startswith("UC"):
        meta_url    = f"https://www.youtube.com/channel/{channel_id}/featured"
        videos_url  = f"https://www.youtube.com/channel/{channel_id}/videos"
    elif channel_id.startswith("@"):
        meta_url    = f"https://www.youtube.com/{channel_id}/featured"
        videos_url  = f"https://www.youtube.com/{channel_id}/videos"
    else:
        meta_url    = f"https://www.youtube.com/channel/{channel_id}/featured"
        videos_url  = f"https://www.youtube.com/channel/{channel_id}/videos"
    
    PROCESSING_IDS.add(f"featured_{channel_id}")
    try:
        loop = asyncio.get_event_loop()
        
        # メタデータ取得（featured URL からチャンネル情報だけ取る）
        def fetch_meta():
            opts = {
                **ydl_opts_base,
                "extract_flat": True,
                "playlistend": 1,
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(meta_url, download=False)
        
        # 動画リスト取得（/videos から確実に取る）
        def fetch_videos():
            opts = {
                **ydl_opts_flat,
                "playlist_items": "1-12",
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(videos_url, download=False)
        
        # 並列実行
        meta_info, videos_info = await asyncio.gather(
            loop.run_in_executor(executor, fetch_meta),
            loop.run_in_executor(executor, fetch_videos),
        )
        
        if not meta_info:
            raise HTTPException(status_code=404, detail="Channel not found")
        
        # @handleをuploader_urlから取得
        handle = None
        uploader_url = meta_info.get("uploader_url", "")
        if "/@" in uploader_url:
            handle = "@" + uploader_url.split("/@")[1].split("/")[0]
        
        # 動画リストを構築
        featured_videos = []
        entries = videos_info.get("entries", []) if videos_info else []
        for e in entries[:12]:
            if not e or not e.get("id"):
                continue
            featured_videos.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
            })
        
        channel_data = {
            "channel_id": meta_info.get("channel_id") or meta_info.get("id") or channel_id,
            "handle": handle,
            "name": meta_info.get("channel") or meta_info.get("uploader") or meta_info.get("title"),
            "icon": get_best_thumbnail(meta_info.get("thumbnails")),
            "subscriber_count": meta_info.get("channel_follower_count") or meta_info.get("subscriber_count"),
            "description": meta_info.get("description"),
            "featured_videos": featured_videos,
            "video_count": len(featured_videos),
        }
        
        CHANNEL_FEATURED_CACHE[channel_id] = (time.time(), channel_data, 3600)
        return channel_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching featured: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        PROCESSING_IDS.discard(f"featured_{channel_id}")

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
        shorts = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "thumbnail": get_best_thumbnail(e.get("thumbnails")),
                "view_count": e.get("view_count"),
                "duration": e.get("duration")
            }
            for e in info.get("entries", []) if e
        ]
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
