from flask import Flask, request, jsonify
from flask_cors import CORS
import instaloader
import re
import time
import logging
import os
import base64
from dotenv import load_dotenv 
from functools import wraps
from collections import defaultdict

load_dotenv() 
app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    quiet=True
)

logger.info("Instaloader instance created")


# --- Session Load from ENV (Render sathi) ---
def load_session_from_env():
    username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    session_b64 = os.environ.get("INSTAGRAM_SESSION_B64", "").strip()

    if not username or not session_b64:
        logger.warning("INSTAGRAM_USERNAME or INSTAGRAM_SESSION_B64 env var missing — unauthenticated mode")
        return False

    try:
        session_data = base64.b64decode(session_b64)

        # Linux (Render) var path
        session_dir = os.path.expanduser("~/.config/instaloader")
        os.makedirs(session_dir, exist_ok=True)
        session_path = os.path.join(session_dir, f"session-{username}")

        with open(session_path, "wb") as f:
            f.write(session_data)

        L.load_session_from_file(username, session_path)
        logger.info(f"Session loaded successfully for @{username}")
        return True

    except Exception as e:
        logger.error(f"Session load failed: {e}")
        return False


# App start hoताना session load kar
session_loaded = load_session_from_env()
if not session_loaded:
    logger.warning("Running WITHOUT session — may fail on datacenter IPs (Render)")


# --- Simple In-Memory Rate Limiter ---
request_counts = defaultdict(list)

def rate_limit(max_requests=10, window_seconds=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr
            now = time.time()
            request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
            current_count = len(request_counts[ip])
            if current_count >= max_requests:
                logger.warning(f"[RateLimit] BLOCKED IP={ip}")
                return jsonify({
                    "error": f"Jast requests! {window_seconds} seconds nantar try kara.",
                    "retry_after": window_seconds
                }), 429
            request_counts[ip].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# --- URL Parsers ---
def extract_shortcode(url):
    url = url.split('?')[0].rstrip('/')
    pattern = r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)"
    match = re.search(pattern, url)
    shortcode = match.group(1) if match else None
    logger.debug(f"[Parser] extract_shortcode({url!r}) → {shortcode!r}")
    return shortcode

def extract_story_info(url):
    pattern = r"/stories/([A-Za-z0-9_\.]+)/?([0-9]+)?"
    match = re.search(pattern, url)
    if match:
        return {"username": match.group(1), "story_id": match.group(2)}
    return None

def extract_username_from_profile_url(url):
    url = url.split('?')[0].rstrip('/')
    pattern = r"instagram\.com/([A-Za-z0-9_\.]+)/?$"
    match = re.search(pattern, url)
    return match.group(1) if match else None

def detect_url_type(url):
    if "/stories/" in url:
        return "story"
    elif "/p/" in url or "/reel/" in url or "/tv/" in url:
        return "post"
    elif re.search(r"instagram\.com/[A-Za-z0-9_\.]+/?$", url):
        return "profile"
    return "unknown"


# --- Media Builders ---
def build_post_response(post):
    logger.info(f"[Builder] shortcode={post.shortcode!r} typename={post.typename!r}")

    base_info = {
        "shortcode": post.shortcode,
        "caption": post.caption[:300] if post.caption else "",
        "likes": post.likes,
        "is_video": post.is_video,
        "timestamp": post.date_utc.isoformat(),
        "owner": post.owner_username,
        "thumbnail": post.url
    }

    if post.typename == "GraphSidecar":
        media_list = []
        for i, node in enumerate(post.get_sidecar_nodes()):
            item_type = "video" if node.is_video else "image"
            media_list.append({
                "type": item_type,
                "url": node.video_url if node.is_video else node.display_url,
                "thumbnail": node.display_url
            })
        return {
            "success": True,
            "type": "carousel",
            "count": len(media_list),
            "info": base_info,
            "data": media_list
        }

    elif post.is_video:
        return {
            "success": True,
            "type": "video",
            "info": base_info,
            "data": [{
                "type": "video",
                "url": post.video_url,
                "thumbnail": post.url,
                "views": post.video_view_count
            }]
        }

    else:
        return {
            "success": True,
            "type": "image",
            "info": base_info,
            "data": [{"type": "image", "url": post.url}]
        }


# ============================================================
# ROUTES
# ============================================================

@app.route('/api/download', methods=['POST'])
@rate_limit(max_requests=15, window_seconds=60)
def download_media():
    ip = request.remote_addr
    logger.info(f"[Request] POST /api/download from IP={ip}")

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    url = data.get('url', '').strip()
    if not url:
        return jsonify({"error": "URL dya!"}), 400

    if "instagram.com" not in url:
        return jsonify({"error": "Fakt Instagram links support hotat!"}), 400

    url_type = detect_url_type(url)
    logger.info(f"[Request] URL={url!r} type={url_type!r}")
    t_start = time.time()

    try:
        # ---- STORY ----
        if url_type == "story":
            story_info = extract_story_info(url)
            if not story_info:
                return jsonify({"error": "Story URL correct nahiye!"}), 400

            if not L.context.is_logged_in:
                return jsonify({"error": "Stories sathi login required ahe. Server madhe session configure kara."}), 403

            try:
                profile = instaloader.Profile.from_username(L.context, story_info["username"])
            except Exception as e:
                logger.error(f"[Story] Profile lookup failed: {e}")
                return jsonify({"error": f"'{story_info['username']}' he account sapadla nahi!"}), 404

            story_links = []
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if story_info["story_id"] and str(item.mediaid) != story_info["story_id"]:
                        continue
                    item_type = "video" if item.is_video else "image"
                    story_links.append({
                        "type": item_type,
                        "url": item.video_url if item.is_video else item.url,
                        "timestamp": item.date_utc.isoformat()
                    })

            if not story_links:
                return jsonify({"error": "Story sapadli nahi! Expire zali asel kiva private ahe."}), 404

            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Story] Done — {len(story_links)} items in {elapsed}s")
            return jsonify({
                "success": True,
                "type": "story",
                "count": len(story_links),
                "data": story_links
            })

        # ---- POST / REEL / CAROUSEL ----
        elif url_type == "post":
            shortcode = extract_shortcode(url)
            if not shortcode:
                return jsonify({"error": "Valid Instagram post/reel URL nahi!"}), 400

            logger.info(f"[Post] Fetching shortcode={shortcode!r}...")
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except instaloader.exceptions.InstaloaderException as e:
                err = str(e)
                if "401" in err or "login" in err.lower():
                    return jsonify({"error": "Instagram ne block kela — session expire zala asel. Session regenerate kara."}), 403
                if "404" in err:
                    return jsonify({"error": "Post sapadla nahi — delete zala asel kiva private ahe."}), 404
                logger.error(f"[Post] InstaloaderException: {e}")
                return jsonify({"error": f"Instagram error: {err}"}), 500

            response = build_post_response(post)
            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Post] Done — type={response['type']!r} in {elapsed}s")
            return jsonify(response)

        # ---- PROFILE ----
        elif url_type == "profile":
            username = extract_username_from_profile_url(url)
            if not username:
                return jsonify({"error": "Valid profile URL nahi!"}), 400

            try:
                profile = instaloader.Profile.from_username(L.context, username)
            except Exception as e:
                logger.error(f"[Profile] Lookup failed for {username!r}: {e}")
                return jsonify({"error": f"'{username}' he account sapadla nahi!"}), 404

            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Profile] Done in {elapsed}s")
            return jsonify({
                "success": True,
                "type": "profile",
                "info": {
                    "username": profile.username,
                    "full_name": profile.full_name,
                    "followers": profile.followers,
                    "following": profile.followees,
                    "posts": profile.mediacount,
                    "bio": profile.biography,
                    "is_private": profile.is_private,
                    "is_verified": profile.is_verified
                },
                "data": [{"type": "image", "url": profile.profile_pic_url}]
            })

        else:
            return jsonify({"error": "He URL support hot nahi! Post/Reel/Story/Profile URL dya."}), 400

    except instaloader.exceptions.InstaloaderException as e:
        error_msg = str(e)
        elapsed = round(time.time() - t_start, 2)
        if "rate" in error_msg.lower() or "429" in error_msg:
            return jsonify({"error": "Instagram ne temporarily block kela. 5-10 minutes nantar try kara!"}), 429
        elif "private" in error_msg.lower():
            return jsonify({"error": "He account private ahe!"}), 403
        logger.error(f"[Instagram] Error after {elapsed}s: {error_msg}")
        return jsonify({"error": f"Instagram error: {error_msg}"}), 500

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        logger.error(f"[Server] Unexpected error after {elapsed}s: {e}", exc_info=True)
        return jsonify({"error": "Server error aala. Thoda vel nantar try kara."}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    logged_in = L.context.is_logged_in
    logger.info(f"[Health] logged_in={logged_in}")
    return jsonify({
        "status": "ok",
        "logged_in": logged_in,
        "session_user": os.environ.get("INSTAGRAM_USERNAME", "not set")
    })


if __name__ == '__main__':
    logger.info("Starting Flask server on 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)