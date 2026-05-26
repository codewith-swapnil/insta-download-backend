from flask import Flask, request, jsonify
from flask_cors import CORS
import instaloader
import re
import time
import logging
from functools import wraps
from collections import defaultdict

# --- Setup ---
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
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

# --- Optional Login (Stories sathi required) ---
def login_instagram(username=None, password=None):
    if not username or not password:
        logger.warning("Login credentials not provided. Stories won't work.")
        return False
    try:
        L.login(username, password)
        logger.info("Instagram Login Successful!")
        return True
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return False

# login_instagram("YOUR_USERNAME", "YOUR_PASSWORD")

# --- Simple In-Memory Rate Limiter ---
request_counts = defaultdict(list)

def rate_limit(max_requests=10, window_seconds=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr
            now = time.time()
            # Juna requests clean kara
            request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
            if len(request_counts[ip]) >= max_requests:
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
    """Post, Reel, TV shortcode kadhto"""
    # Clean up URL
    url = url.split('?')[0].rstrip('/')
    pattern = r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None

def extract_story_info(url):
    """Story URL madhun username ani story_id kadhto"""
    pattern = r"/stories/([A-Za-z0-9_\.]+)/?([0-9]+)?"
    match = re.search(pattern, url)
    if match:
        return {
            "username": match.group(1),
            "story_id": match.group(2)
        }
    return None

def extract_username_from_profile_url(url):
    """Profile URL madhun username kadhto"""
    url = url.split('?')[0].rstrip('/')
    pattern = r"instagram\.com/([A-Za-z0-9_\.]+)/?$"
    match = re.search(pattern, url)
    return match.group(1) if match else None

def detect_url_type(url):
    """URL konata type ahe te detect karto"""
    if "/stories/" in url:
        return "story"
    elif "/p/" in url or "/reel/" in url or "/tv/" in url:
        return "post"
    elif re.search(r"instagram\.com/[A-Za-z0-9_\.]+/?$", url):
        return "profile"
    return "unknown"

# --- Media Builders ---
def build_post_response(post):
    """Post madhun clean response banvto"""
    base_info = {
        "shortcode": post.shortcode,
        "caption": post.caption[:300] if post.caption else "",
        "likes": post.likes,
        "is_video": post.is_video,
        "timestamp": post.date_utc.isoformat(),
        "owner": post.owner_username,
        "thumbnail": post.url  # Always image URL (thumbnail for videos pan)
    }

    # 1. Carousel (Multiple items)
    if post.typename == "GraphSidecar":
        media_list = []
        for node in post.get_sidecar_nodes():
            media_list.append({
                "type": "video" if node.is_video else "image",
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

    # 2. Single Video / Reel
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

    # 3. Single Photo
    else:
        return {
            "success": True,
            "type": "image",
            "info": base_info,
            "data": [{
                "type": "image",
                "url": post.url
            }]
        }

# ============================================================
# ROUTES
# ============================================================

@app.route('/api/download', methods=['POST'])
@rate_limit(max_requests=15, window_seconds=60)
def download_media():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    url = data.get('url', '').strip()
    if not url:
        return jsonify({"error": "URL dya!"}), 400

    # Basic URL validation
    if "instagram.com" not in url:
        return jsonify({"error": "Fakt Instagram links support hotat!"}), 400

    url_type = detect_url_type(url)
    logger.info(f"Processing {url_type} URL: {url}")

    try:
        # ---- STORY ----
        if url_type == "story":
            story_info = extract_story_info(url)
            if not story_info:
                return jsonify({"error": "Story URL correct nahiye!"}), 400

            try:
                profile = instaloader.Profile.from_username(L.context, story_info["username"])
            except Exception:
                return jsonify({"error": f"'{story_info['username']}' he account sapadla nahi!"}), 404

            story_links = []
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if story_info["story_id"] and str(item.mediaid) != story_info["story_id"]:
                        continue
                    story_links.append({
                        "type": "video" if item.is_video else "image",
                        "url": item.video_url if item.is_video else item.url,
                        "timestamp": item.date_utc.isoformat()
                    })

            if not story_links:
                return jsonify({
                    "error": "Story sapadli nahi! Story expire zali asel kiva account private ahe. Login check kara."
                }), 404

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

            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except instaloader.exceptions.InstaloaderException as e:
                if "Login" in str(e) or "login" in str(e):
                    return jsonify({"error": "He post private ahe! Login karnya sathi server configure kara."}), 403
                return jsonify({"error": "Post sapadla nahi! Delete zala asel kiva private ahe."}), 404

            return jsonify(build_post_response(post))

        # ---- PROFILE PIC ----
        elif url_type == "profile":
            username = extract_username_from_profile_url(url)
            if not username:
                return jsonify({"error": "Valid profile URL nahi!"}), 400

            try:
                profile = instaloader.Profile.from_username(L.context, username)
            except Exception:
                return jsonify({"error": f"'{username}' he account sapadla nahi!"}), 404

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
                "data": [{
                    "type": "image",
                    "url": profile.profile_pic_url
                }]
            })

        else:
            return jsonify({"error": "He URL support hot nahi! Post/Reel/Story/Profile URL dya."}), 400

    except instaloader.exceptions.InstaloaderException as e:
        error_msg = str(e)
        if "rate" in error_msg.lower() or "429" in error_msg:
            return jsonify({"error": "Instagram ne temporarily block kela. 5-10 minutes nantar try kara!"}), 429
        elif "Private" in error_msg or "private" in error_msg:
            return jsonify({"error": "He account private ahe!"}), 403
        logger.error(f"Instaloader error: {e}")
        return jsonify({"error": f"Instagram error: {error_msg}"}), 500

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({"error": "Server error aala. Thoda vel nantar try kara."}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "logged_in": L.context.is_logged_in})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)