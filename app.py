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

logger.info("Instaloader instance created (no downloads configured)")

# Session load kara
# try:
#     L.load_session_from_file("me_rohan_jadhav", "session-me_rohan_jadhav")
#     logger.info("Session loaded successfully!")
# except Exception as e:
#     logger.warning(f"Session load failed: {e}")


# --- Optional Login ---
def login_instagram(username=None, password=None):
    if not username or not password:
        logger.warning("Login credentials not provided — stories won't work")
        return False
    try:
        logger.info(f"Attempting Instagram login for user: {username}")
        L.login(username, password)
        logger.info("Instagram login successful!")
        return True
    except Exception as e:
        logger.error(f"Instagram login failed: {e}")
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
            request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
            current_count = len(request_counts[ip])
            logger.debug(f"[RateLimit] IP={ip} | requests in window={current_count}/{max_requests}")
            if current_count >= max_requests:
                logger.warning(f"[RateLimit] BLOCKED IP={ip} — {current_count} requests in {window_seconds}s")
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
        result = {"username": match.group(1), "story_id": match.group(2)}
        logger.debug(f"[Parser] extract_story_info → {result}")
        return result
    logger.debug(f"[Parser] extract_story_info → no match for {url!r}")
    return None

def extract_username_from_profile_url(url):
    url = url.split('?')[0].rstrip('/')
    pattern = r"instagram\.com/([A-Za-z0-9_\.]+)/?$"
    match = re.search(pattern, url)
    username = match.group(1) if match else None
    logger.debug(f"[Parser] extract_username_from_profile_url({url!r}) → {username!r}")
    return username

def detect_url_type(url):
    if "/stories/" in url:
        result = "story"
    elif "/p/" in url or "/reel/" in url or "/tv/" in url:
        result = "post"
    elif re.search(r"instagram\.com/[A-Za-z0-9_\.]+/?$", url):
        result = "profile"
    else:
        result = "unknown"
    logger.debug(f"[Parser] detect_url_type({url!r}) → {result!r}")
    return result


# --- Media Builders ---
def build_post_response(post):
    logger.info(f"[Builder] Building response for post shortcode={post.shortcode!r} typename={post.typename!r}")

    base_info = {
        "shortcode": post.shortcode,
        "caption": post.caption[:300] if post.caption else "",
        "likes": post.likes,
        "is_video": post.is_video,
        "timestamp": post.date_utc.isoformat(),
        "owner": post.owner_username,
        "thumbnail": post.url
    }
    logger.info(f"[Builder] Post owner={post.owner_username!r} likes={post.likes} is_video={post.is_video}")

    # 1. Carousel
    if post.typename == "GraphSidecar":
        logger.info("[Builder] Post type: CAROUSEL — fetching sidecar nodes...")
        media_list = []
        for i, node in enumerate(post.get_sidecar_nodes()):
            item_type = "video" if node.is_video else "image"
            media_list.append({
                "type": item_type,
                "url": node.video_url if node.is_video else node.display_url,
                "thumbnail": node.display_url
            })
            logger.info(f"[Builder] Carousel item {i+1}: type={item_type!r}")
        logger.info(f"[Builder] Carousel done — total items={len(media_list)}")
        return {
            "success": True,
            "type": "carousel",
            "count": len(media_list),
            "info": base_info,
            "data": media_list
        }

    # 2. Single Video / Reel
    elif post.is_video:
        logger.info(f"[Builder] Post type: VIDEO — views={post.video_view_count}")
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
        logger.info("[Builder] Post type: IMAGE")
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
        logger.warning(f"[Request] IP={ip} — request body is empty or not JSON")
        return jsonify({"error": "JSON body required"}), 400

    url = data.get('url', '').strip()
    if not url:
        logger.warning(f"[Request] IP={ip} — 'url' field missing or empty")
        return jsonify({"error": "URL dya!"}), 400

    logger.info(f"[Request] IP={ip} URL={url!r}")

    if "instagram.com" not in url:
        logger.warning(f"[Request] IP={ip} — rejected non-Instagram URL: {url!r}")
        return jsonify({"error": "Fakt Instagram links support hotat!"}), 400

    url_type = detect_url_type(url)
    logger.info(f"[Request] URL type resolved: {url_type!r}")

    t_start = time.time()

    try:
        # ---- STORY ----
        if url_type == "story":
            logger.info("[Story] Processing story URL...")
            story_info = extract_story_info(url)
            if not story_info:
                logger.warning("[Story] Failed to extract story info from URL")
                return jsonify({"error": "Story URL correct nahiye!"}), 400

            logger.info(f"[Story] Username={story_info['username']!r} StoryID={story_info['story_id']!r}")
            logger.info(f"[Story] Fetching profile for @{story_info['username']}...")

            try:
                profile = instaloader.Profile.from_username(L.context, story_info["username"])
                logger.info(f"[Story] Profile found: userid={profile.userid} is_private={profile.is_private}")
            except Exception as e:
                logger.error(f"[Story] Profile lookup failed for {story_info['username']!r}: {e}")
                return jsonify({"error": f"'{story_info['username']}' he account sapadla nahi!"}), 404

            logger.info(f"[Story] Fetching stories for userid={profile.userid}...")
            story_links = []
            for story in L.get_stories(userids=[profile.userid]):
                logger.info(f"[Story] Processing story batch for user={story.owner_id}")
                for item in story.get_items():
                    if story_info["story_id"] and str(item.mediaid) != story_info["story_id"]:
                        logger.debug(f"[Story] Skipping item mediaid={item.mediaid} (target={story_info['story_id']})")
                        continue
                    item_type = "video" if item.is_video else "image"
                    story_links.append({
                        "type": item_type,
                        "url": item.video_url if item.is_video else item.url,
                        "timestamp": item.date_utc.isoformat()
                    })
                    logger.info(f"[Story] Found item: type={item_type!r} mediaid={item.mediaid} ts={item.date_utc.isoformat()}")

            if not story_links:
                logger.warning(f"[Story] No story items found for @{story_info['username']} (expired or private?)")
                return jsonify({
                    "error": "Story sapadli nahi! Story expire zali asel kiva account private ahe. Login check kara."
                }), 404

            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Story] Done — {len(story_links)} items returned in {elapsed}s")
            return jsonify({
                "success": True,
                "type": "story",
                "count": len(story_links),
                "data": story_links
            })

        # ---- POST / REEL / CAROUSEL ----
        elif url_type == "post":
            logger.info("[Post] Processing post/reel URL...")
            shortcode = extract_shortcode(url)
            if not shortcode:
                logger.warning(f"[Post] Could not extract shortcode from: {url!r}")
                return jsonify({"error": "Valid Instagram post/reel URL nahi!"}), 400

            logger.info(f"[Post] Shortcode={shortcode!r} — fetching from Instagram...")
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                logger.info(f"[Post] Post fetched: owner={post.owner_username!r} type={post.typename!r} is_video={post.is_video}")
            except instaloader.exceptions.InstaloaderException as e:
                if "Login" in str(e) or "login" in str(e):
                    logger.warning(f"[Post] Login required for shortcode={shortcode!r}: {e}")
                    return jsonify({"error": "He post private ahe! Login karnya sathi server configure kara."}), 403
                logger.error(f"[Post] Instaloader error fetching shortcode={shortcode!r}: {e}")
                return jsonify({"error": "Post sapadla nahi! Delete zala asel kiva private ahe."}), 404

            response = build_post_response(post)
            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Post] Done — type={response['type']!r} in {elapsed}s")
            return jsonify(response)

        # ---- PROFILE PIC ----
        elif url_type == "profile":
            logger.info("[Profile] Processing profile URL...")
            username = extract_username_from_profile_url(url)
            if not username:
                logger.warning(f"[Profile] Could not extract username from: {url!r}")
                return jsonify({"error": "Valid profile URL nahi!"}), 400

            logger.info(f"[Profile] Username={username!r} — fetching profile data...")
            try:
                profile = instaloader.Profile.from_username(L.context, username)
                logger.info(
                    f"[Profile] Fetched: full_name={profile.full_name!r} "
                    f"followers={profile.followers} posts={profile.mediacount} "
                    f"is_private={profile.is_private} is_verified={profile.is_verified}"
                )
            except Exception as e:
                logger.error(f"[Profile] Lookup failed for {username!r}: {e}")
                return jsonify({"error": f"'{username}' he account sapadla nahi!"}), 404

            logger.info(f"[Profile] Profile pic URL retrieved for @{username}")
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
            logger.warning(f"[Request] Unsupported URL type={url_type!r} for URL={url!r}")
            return jsonify({"error": "He URL support hot nahi! Post/Reel/Story/Profile URL dya."}), 400

    except instaloader.exceptions.InstaloaderException as e:
        error_msg = str(e)
        elapsed = round(time.time() - t_start, 2)
        if "rate" in error_msg.lower() or "429" in error_msg:
            logger.warning(f"[Instagram] Rate limited by Instagram after {elapsed}s: {error_msg}")
            return jsonify({"error": "Instagram ne temporarily block kela. 5-10 minutes nantar try kara!"}), 429
        elif "Private" in error_msg or "private" in error_msg:
            logger.warning(f"[Instagram] Private account error after {elapsed}s: {error_msg}")
            return jsonify({"error": "He account private ahe!"}), 403
        logger.error(f"[Instagram] Instaloader error after {elapsed}s: {error_msg}")
        return jsonify({"error": f"Instagram error: {error_msg}"}), 500

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        logger.error(f"[Server] Unexpected error after {elapsed}s: {e}", exc_info=True)
        return jsonify({"error": "Server error aala. Thoda vel nantar try kara."}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    logged_in = L.context.is_logged_in
    logger.info(f"[Health] GET /api/health — logged_in={logged_in}")
    return jsonify({"status": "ok", "logged_in": logged_in})


if __name__ == '__main__':
    logger.info("Starting Flask server on 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)