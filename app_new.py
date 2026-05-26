from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import logging
import os
import time
from collections import defaultdict
from functools import wraps

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate Limiter (15 requests per 60 seconds per IP)
request_counts = defaultdict(list)

def rate_limit(max_requests=15, window_seconds=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr
            now = time.time()
            # Clean old entries
            request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
            if len(request_counts[ip]) >= max_requests:
                return jsonify({"error": "Too many requests! Please try after 60 seconds."}), 429
            request_counts[ip].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def get_ydl_opts():
    """Return yt-dlp options with optional Instagram credentials."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,          # Don't download file, just get URLs
        'ignoreerrors': True,            # Skip unavailable entries in carousel
    }
    
    # Optional Instagram login (helps with private/age-restricted content)
    ig_user = os.environ.get("IG_USERNAME")
    ig_pass = os.environ.get("IG_PASSWORD")
    if ig_user and ig_pass:
        opts['username'] = ig_user
        opts['password'] = ig_pass
        logger.info("Instagram credentials loaded from environment")
    else:
        logger.info("No Instagram credentials provided – some carousels may fail")
    
    return opts

def extract_media_info(url):
    """Extract media info using yt-dlp."""
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        return info

def extract_single_item(item_url):
    """Helper to recursively extract a single carousel item if initial extraction was shallow."""
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            return ydl.extract_info(item_url, download=False)
    except Exception as e:
        logger.error(f"Failed to re-extract item {item_url}: {e}")
        return None

@app.route('/api/download', methods=['POST'])
@rate_limit(max_requests=15, window_seconds=60)
def download_media():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    url = data.get('url', '').strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    if "instagram.com" not in url:
        return jsonify({"error": "Only Instagram links are supported"}), 400

    # Clean URL: remove tracking parameters
    url = url.split('?')[0]

    logger.info(f"Processing URL: {url}")

    try:
        info = extract_media_info(url)
        
        if not info:
            return jsonify({"error": "No media found. URL might be invalid or private."}), 404

        # ---------------------------
        # Case 1: Carousel (multiple images/videos)
        # ---------------------------
        if 'entries' in info:
            entries = info['entries']
            logger.info(f"Carousel detected with {len(entries)} entries")
            
            media_list = []
            
            for idx, entry in enumerate(entries):
                if not entry:
                    logger.warning(f"Entry {idx} is None – skipping")
                    continue
                
                # If entry has no URL but has a webpage_url, try to extract it fully
                if not entry.get('url') and entry.get('webpage_url'):
                    logger.info(f"Re-extracting entry {idx}: {entry['webpage_url']}")
                    full_entry = extract_single_item(entry['webpage_url'])
                    if full_entry:
                        entry = full_entry
                    else:
                        continue
                
                # Determine media type and URL
                media_url = None
                media_type = None
                
                # Prefer video formats
                if entry.get('formats'):
                    # Find best video format (mp4 preferred)
                    video_formats = [f for f in entry['formats'] 
                                    if f.get('vcodec') != 'none' 
                                    and f.get('url') 
                                    and f.get('ext') in ['mp4', 'mov', 'webm']]
                    if video_formats:
                        best_video = video_formats[-1]  # last is usually highest quality
                        media_url = best_video['url']
                        media_type = "video"
                    else:
                        # No video? Use first available format (likely image)
                        media_url = entry['formats'][0]['url']
                        media_type = "image"
                else:
                    # Direct URL (image or video file)
                    media_url = entry.get('url')
                    # Guess type from URL extension
                    if media_url and any(ext in media_url.lower() for ext in ['.mp4', '.mov', '.webm']):
                        media_type = "video"
                    else:
                        media_type = "image"
                
                if not media_url:
                    logger.warning(f"Entry {idx} has no usable URL – skipping")
                    continue
                
                media_list.append({
                    "type": media_type,
                    "url": media_url,
                    "thumbnail": entry.get('thumbnail', '')
                })
            
            if not media_list:
                return jsonify({"error": "No downloadable media found in carousel"}), 404
            
            return jsonify({
                "success": True,
                "type": "carousel",
                "count": len(media_list),
                "info": {
                    "caption": info.get('title', '') or info.get('description', ''),
                    "owner": info.get('uploader', ''),
                    "timestamp": info.get('upload_date', '')
                },
                "data": media_list
            })
        
        # ---------------------------
        # Case 2: Single video or reel
        # ---------------------------
        elif info.get('formats'):
            formats = info.get('formats', [])
            
            # Find best video format (mp4)
            video_formats = [f for f in formats 
                           if f.get('vcodec') != 'none' 
                           and f.get('url')
                           and f.get('ext') in ['mp4', 'webm', 'mov']]
            
            if video_formats:
                best = video_formats[-1]
                return jsonify({
                    "success": True,
                    "type": "video",
                    "info": {
                        "caption": info.get('title', ''),
                        "owner": info.get('uploader', ''),
                        "likes": info.get('like_count', 0),
                        "views": info.get('view_count', 0),
                        "timestamp": info.get('upload_date', '')
                    },
                    "data": [{
                        "type": "video",
                        "url": best['url'],
                        "thumbnail": info.get('thumbnail', '')
                    }]
                })
            else:
                # No video format? Might be an image with formats
                if formats:
                    return jsonify({
                        "success": True,
                        "type": "image",
                        "info": {
                            "caption": info.get('title', ''),
                            "owner": info.get('uploader', ''),
                            "timestamp": info.get('upload_date', '')
                        },
                        "data": [{
                            "type": "image",
                            "url": formats[0]['url'],
                            "thumbnail": info.get('thumbnail', '')
                        }]
                    })
        
        # ---------------------------
        # Case 3: Single image
        # ---------------------------
        if info.get('url'):
            return jsonify({
                "success": True,
                "type": "image",
                "info": {
                    "caption": info.get('title', ''),
                    "owner": info.get('uploader', ''),
                    "timestamp": info.get('upload_date', '')
                },
                "data": [{
                    "type": "image",
                    "url": info['url'],
                    "thumbnail": info.get('thumbnail', '')
                }]
            })
        
        # If we reach here, nothing was found
        return jsonify({"error": "Could not extract any media URL from this post"}), 404

    except yt_dlp.utils.DownloadError as e:
        error_str = str(e).lower()
        logger.error(f"yt-dlp error: {e}")
        
        if "private" in error_str or "login" in error_str:
            return jsonify({"error": "This post is private. Please set IG_USERNAME and IG_PASSWORD environment variables."}), 403
        elif "not found" in error_str or "404" in error_str:
            return jsonify({"error": "Post not found. It may have been deleted."}), 404
        elif "rate" in error_str or "429" in error_str:
            return jsonify({"error": "Rate limited by Instagram. Please try again later."}), 429
        else:
            return jsonify({"error": f"Download failed: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again later."}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "engine": "yt-dlp",
        "instagram_login": bool(os.environ.get("IG_USERNAME"))
    })

if __name__ == '__main__':
    # Run on all interfaces, port 5000
    app.run(host='0.0.0.0', port=5000, debug=False)