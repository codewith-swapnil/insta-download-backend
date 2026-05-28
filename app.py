"""
Instagram Media Downloader — Flask API
Architecture: Rotating Residential Proxy Pool → Instagram
Author: Senior Dev / Network Solutions Arch

Proxy Services Supported:
  - Webshare.io (recommended)         → PROXY_LIST env var (newline-separated)
  - Single gateway (Bright Data etc.) → PROXY_GATEWAY env var
  - Auto-fallback to direct if no proxies (dev mode)

ENV VARS:
  INSTAGRAM_USERNAME       — Instagram account username
  INSTAGRAM_SESSION_B64    — base64-encoded instaloader session file
  PROXY_LIST               — Newline or comma-separated proxies
                             Format: http://user:pass@host:port
  PROXY_GATEWAY            — Single rotating gateway URL (alternative to PROXY_LIST)
  PROXY_BLACKLIST_TTL      — Seconds to blacklist a bad proxy (default: 300)
  MAX_RETRIES              — Retry attempts per request (default: 3)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import instaloader
import re
import time
import logging
import os
import base64
import random
import threading
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

# ============================================================
# PROXY MANAGER — Rotating Residential IP Pool
# ============================================================

class ProxyManager:
    """
    Thread-safe rotating proxy pool with:
      - Round-robin selection
      - Per-proxy failure tracking
      - Temporary blacklisting (TTL-based)
      - Auto-recovery after blacklist TTL expires
      - Health score per proxy
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proxies: list[str] = []
        self._index: int = 0
        self._failures: dict[str, int] = defaultdict(int)          # proxy → fail count
        self._blacklisted: dict[str, float] = {}                   # proxy → expiry timestamp
        self._blacklist_ttl: int = int(os.environ.get("PROXY_BLACKLIST_TTL", 300))
        self._load()

    def _load(self):
        """Load proxies from PROXY_LIST or PROXY_GATEWAY env vars."""

        # Option A: Single rotating gateway (Bright Data / Oxylabs / SmartProxy style)
        gateway = os.environ.get("PROXY_GATEWAY", "").strip()
        if gateway:
            self._proxies = [gateway]
            logger.info(f"[ProxyManager] Loaded 1 gateway proxy: {self._mask(gateway)}")
            return

        # Option B: Explicit proxy list (Webshare.io style)
        raw = os.environ.get("PROXY_LIST", "").strip()
        if raw:
            # Support newline OR comma separated
            entries = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
            self._proxies = entries
            logger.info(f"[ProxyManager] Loaded {len(self._proxies)} proxies from PROXY_LIST")
            for p in self._proxies[:3]:
                logger.info(f"  → {self._mask(p)}")
            if len(self._proxies) > 3:
                logger.info(f"  ... and {len(self._proxies) - 3} more")
            return

        logger.warning(
            "[ProxyManager] No proxies configured! "
            "Set PROXY_LIST or PROXY_GATEWAY env var. Running DIRECT (may get blocked on Render)."
        )

    def _mask(self, proxy: str) -> str:
        """Mask credentials in proxy URL for logging."""
        return re.sub(r"://([^:]+):([^@]+)@", r"://***:***@", proxy)

    def _is_blacklisted(self, proxy: str) -> bool:
        expiry = self._blacklisted.get(proxy)
        if expiry and time.time() < expiry:
            return True
        if expiry:
            # TTL expired — remove from blacklist
            del self._blacklisted[proxy]
            self._failures[proxy] = 0
            logger.info(f"[ProxyManager] Proxy recovered from blacklist: {self._mask(proxy)}")
        return False

    def get_proxy(self) -> dict | None:
        """
        Returns a requests-compatible proxy dict or None if no proxies available.
        Uses round-robin, skipping blacklisted proxies.
        """
        with self._lock:
            if not self._proxies:
                return None

            total = len(self._proxies)
            tried = 0
            while tried < total:
                proxy = self._proxies[self._index % total]
                self._index = (self._index + 1) % total
                if not self._is_blacklisted(proxy):
                    return {"http": proxy, "https": proxy}
                tried += 1

            # All proxies blacklisted — return random one anyway (best effort)
            proxy = random.choice(self._proxies)
            logger.warning(f"[ProxyManager] All proxies blacklisted! Using {self._mask(proxy)} anyway.")
            return {"http": proxy, "https": proxy}

    def report_failure(self, proxy_dict: dict | None, reason: str = ""):
        """Call when a proxy causes an error. Blacklists after 3 consecutive failures."""
        if not proxy_dict:
            return
        proxy = proxy_dict.get("https") or proxy_dict.get("http")
        if not proxy:
            return
        with self._lock:
            self._failures[proxy] += 1
            fail_count = self._failures[proxy]
            logger.warning(
                f"[ProxyManager] Proxy failure #{fail_count}: {self._mask(proxy)} — {reason}"
            )
            if fail_count >= 3:
                expiry = time.time() + self._blacklist_ttl
                self._blacklisted[proxy] = expiry
                logger.error(
                    f"[ProxyManager] BLACKLISTED proxy {self._mask(proxy)} "
                    f"for {self._blacklist_ttl}s (until {time.strftime('%H:%M:%S', time.localtime(expiry))})"
                )

    def report_success(self, proxy_dict: dict | None):
        """Reset failure count on success."""
        if not proxy_dict:
            return
        proxy = proxy_dict.get("https") or proxy_dict.get("http")
        if proxy and self._failures.get(proxy):
            with self._lock:
                self._failures[proxy] = 0

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._proxies if not self._is_blacklisted(p))

    @property
    def total_count(self) -> int:
        return len(self._proxies)


proxy_manager = ProxyManager()

# ============================================================
# INSTALOADER FACTORY — Creates fresh L instance with proxy
# ============================================================

def make_instaloader(proxy_dict: dict | None = None) -> instaloader.Instaloader:
    """
    Creates an Instaloader instance and injects proxy into its
    underlying requests.Session. Fresh instance per request avoids
    session state bleeding between proxy rotations.
    """
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

    if proxy_dict:
        # Instaloader uses requests.Session internally via L.context._session
        L.context._session.proxies.update(proxy_dict)
        # Also set for urllib3 adapter
        L.context._session.trust_env = False  # Don't inherit system proxies
        masked = proxy_manager._mask(proxy_dict.get("https", ""))
        logger.debug(f"[Instaloader] Proxy injected: {masked}")

    return L


# ============================================================
# SESSION LOADER
# ============================================================

_session_data_bytes: bytes | None = None
_session_username: str = ""

def _preload_session_bytes():
    """Load session bytes once at startup."""
    global _session_data_bytes, _session_username
    username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    session_b64 = os.environ.get("INSTAGRAM_SESSION_B64", "").strip()

    if not username or not session_b64:
        logger.warning("[Session] INSTAGRAM_USERNAME or INSTAGRAM_SESSION_B64 missing — unauthenticated mode")
        return False

    try:
        _session_data_bytes = base64.b64decode(session_b64)
        _session_username = username
        logger.info(f"[Session] Session bytes preloaded for @{username}")
        return True
    except Exception as e:
        logger.error(f"[Session] Failed to decode session: {e}")
        return False


def load_session_into(L: instaloader.Instaloader) -> bool:
    """Injects the preloaded session into the given Instaloader instance."""
    global _session_data_bytes, _session_username

    if not _session_data_bytes or not _session_username:
        return False

    try:
        session_dir = os.path.expanduser("~/.config/instaloader")
        os.makedirs(session_dir, exist_ok=True)
        session_path = os.path.join(session_dir, f"session-{_session_username}")

        with open(session_path, "wb") as f:
            f.write(_session_data_bytes)

        L.load_session_from_file(_session_username, session_path)
        return True
    except Exception as e:
        logger.error(f"[Session] Failed to load into instance: {e}")
        return False


session_available = _preload_session_bytes()
if not session_available:
    logger.warning("[Session] Running WITHOUT session — stories/private content unavailable")


# ============================================================
# RETRY WRAPPER
# ============================================================

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 3))


def with_proxy_retry(fn):
    """
    Decorator: retries fn(L, *args) up to MAX_RETRIES times,
    rotating to a fresh proxy on each Instagram block/error.
    fn must accept `L` as its first argument.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            proxy_dict = proxy_manager.get_proxy()
            L = make_instaloader(proxy_dict)
            if session_available:
                load_session_into(L)

            masked_proxy = proxy_manager._mask(
                (proxy_dict or {}).get("https", "DIRECT")
            )
            logger.info(f"[Retry] Attempt {attempt}/{MAX_RETRIES} via {masked_proxy}")

            try:
                result = fn(L, *args, **kwargs)
                proxy_manager.report_success(proxy_dict)
                return result

            except instaloader.exceptions.InstaloaderException as e:
                err = str(e)
                last_error = e

                # Determine if this is a proxy/IP block vs a real error
                is_ip_block = any(k in err for k in ["429", "401", "rate", "Please wait", "temporarily"])
                is_not_found = "404" in err
                is_private = "private" in err.lower()

                if is_not_found or is_private:
                    # These won't change with proxy rotation — raise immediately
                    raise

                if is_ip_block:
                    logger.warning(f"[Retry] IP block detected on attempt {attempt}: {err[:80]}")
                    proxy_manager.report_failure(proxy_dict, reason=err[:80])
                    if attempt < MAX_RETRIES:
                        time.sleep(1.5 * attempt)  # Exponential backoff
                    continue

                # Other Instagram error — report and retry
                proxy_manager.report_failure(proxy_dict, reason=err[:80])
                if attempt < MAX_RETRIES:
                    time.sleep(1.0)
                continue

            except Exception as e:
                proxy_manager.report_failure(proxy_dict, reason=str(e)[:80])
                last_error = e
                if attempt < MAX_RETRIES:
                    time.sleep(1.0)
                continue

        # All retries exhausted
        raise last_error or Exception("All proxy retries exhausted")

    return wrapper


# ============================================================
# INSTALOADER OPERATIONS (proxy-aware)
# ============================================================

@with_proxy_retry
def fetch_post(L: instaloader.Instaloader, shortcode: str):
    return instaloader.Post.from_shortcode(L.context, shortcode)


@with_proxy_retry
def fetch_profile(L: instaloader.Instaloader, username: str):
    return instaloader.Profile.from_username(L.context, username)


def fetch_story_items(username: str, story_id: str | None) -> list:
    """Stories require session — use session-aware retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        proxy_dict = proxy_manager.get_proxy()
        L = make_instaloader(proxy_dict)
        if not load_session_into(L):
            raise PermissionError("Session required for stories")

        masked = proxy_manager._mask((proxy_dict or {}).get("https", "DIRECT"))
        logger.info(f"[Story] Attempt {attempt}/{MAX_RETRIES} via {masked}")

        try:
            profile = instaloader.Profile.from_username(L.context, username)
            story_links = []
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if story_id and str(item.mediaid) != story_id:
                        continue
                    story_links.append({
                        "type": "video" if item.is_video else "image",
                        "url": item.video_url if item.is_video else item.url,
                        "timestamp": item.date_utc.isoformat()
                    })
            proxy_manager.report_success(proxy_dict)
            return story_links

        except instaloader.exceptions.InstaloaderException as e:
            proxy_manager.report_failure(proxy_dict, reason=str(e)[:80])
            if attempt == MAX_RETRIES:
                raise
            time.sleep(1.5 * attempt)

    return []


# ============================================================
# URL PARSERS
# ============================================================

def extract_shortcode(url: str) -> str | None:
    url = url.split("?")[0].rstrip("/")
    match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None

def extract_story_info(url: str) -> dict | None:
    match = re.search(r"/stories/([A-Za-z0-9_\.]+)/?([0-9]+)?", url)
    return {"username": match.group(1), "story_id": match.group(2)} if match else None

def extract_username_from_profile_url(url: str) -> str | None:
    url = url.split("?")[0].rstrip("/")
    match = re.search(r"instagram\.com/([A-Za-z0-9_\.]+)/?$", url)
    return match.group(1) if match else None

def detect_url_type(url: str) -> str:
    if "/stories/" in url:
        return "story"
    if "/p/" in url or "/reel/" in url or "/tv/" in url:
        return "post"
    if re.search(r"instagram\.com/[A-Za-z0-9_\.]+/?$", url):
        return "profile"
    return "unknown"


# ============================================================
# RESPONSE BUILDERS
# ============================================================

def build_post_response(post) -> dict:
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
        media_list = [
            {
                "type": "video" if node.is_video else "image",
                "url": node.video_url if node.is_video else node.display_url,
                "thumbnail": node.display_url
            }
            for node in post.get_sidecar_nodes()
        ]
        return {"success": True, "type": "carousel", "count": len(media_list), "info": base_info, "data": media_list}

    if post.is_video:
        return {
            "success": True, "type": "video", "info": base_info,
            "data": [{"type": "video", "url": post.video_url, "thumbnail": post.url, "views": post.video_view_count}]
        }

    return {"success": True, "type": "image", "info": base_info, "data": [{"type": "image", "url": post.url}]}


# ============================================================
# RATE LIMITER
# ============================================================

_request_counts: dict[str, list] = defaultdict(list)

def rate_limit(max_requests: int = 15, window_seconds: int = 60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr
            now = time.time()
            _request_counts[ip] = [t for t in _request_counts[ip] if now - t < window_seconds]
            if len(_request_counts[ip]) >= max_requests:
                logger.warning(f"[RateLimit] BLOCKED IP={ip}")
                return jsonify({
                    "error": f"Too many requests! Retry after {window_seconds} seconds.",
                    "retry_after": window_seconds
                }), 429
            _request_counts[ip].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ============================================================
# ROUTES
# ============================================================

@app.route("/api/download", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def download_media():
    ip = request.remote_addr
    logger.info(f"[Request] POST /api/download from IP={ip}")

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required!"}), 400

    if "instagram.com" not in url:
        return jsonify({"error": "Only Instagram links are supported!"}), 400

    url_type = detect_url_type(url)
    logger.info(f"[Request] URL={url!r} type={url_type!r}")
    t_start = time.time()

    try:
        # ---- STORY ----
        if url_type == "story":
            story_info = extract_story_info(url)
            if not story_info:
                return jsonify({"error": "Invalid story URL!"}), 400
            if not session_available:
                return jsonify({"error": "Stories require login. Configure session env vars."}), 403

            story_links = fetch_story_items(story_info["username"], story_info["story_id"])

            if not story_links:
                return jsonify({"error": "Story not found — expired or private."}), 404

            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Story] Done — {len(story_links)} items in {elapsed}s")
            return jsonify({"success": True, "type": "story", "count": len(story_links), "data": story_links})

        # ---- POST / REEL / CAROUSEL ----
        elif url_type == "post":
            shortcode = extract_shortcode(url)
            if not shortcode:
                return jsonify({"error": "Invalid Instagram post/reel URL!"}), 400

            logger.info(f"[Post] Fetching shortcode={shortcode!r}...")

            try:
                post = fetch_post(shortcode)
            except instaloader.exceptions.InstaloaderException as e:
                err = str(e)
                if "401" in err or "login" in err.lower():
                    return jsonify({"error": "Instagram blocked — session expired. Regenerate session.", "debug": err}), 403
                if "404" in err:
                    return jsonify({"error": "Post not found — deleted or private.", "debug": err}), 404
                return jsonify({"error": f"Instagram error: {err}"}), 500

            response = build_post_response(post)
            elapsed = round(time.time() - t_start, 2)
            logger.info(f"[Post] Done — type={response['type']!r} in {elapsed}s")
            return jsonify(response)

        # ---- PROFILE ----
        elif url_type == "profile":
            username = extract_username_from_profile_url(url)
            if not username:
                return jsonify({"error": "Invalid profile URL!"}), 400

            try:
                profile = fetch_profile(username)
            except Exception as e:
                logger.error(f"[Profile] Lookup failed for {username!r}: {e}")
                return jsonify({"error": f"Account '{username}' not found!"}), 404

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
            return jsonify({"error": "Unsupported URL! Provide a Post/Reel/Story/Profile URL."}), 400

    except instaloader.exceptions.InstaloaderException as e:
        error_msg = str(e)
        elapsed = round(time.time() - t_start, 2)
        if "rate" in error_msg.lower() or "429" in error_msg:
            return jsonify({"error": "Instagram rate-limited all proxies. Try again in 5–10 minutes!"}), 429
        if "private" in error_msg.lower():
            return jsonify({"error": "This account is private!"}), 403
        logger.error(f"[Instagram] Error after {elapsed}s: {error_msg}")
        return jsonify({"error": f"Instagram error: {error_msg}"}), 500

    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        logger.error(f"[Server] Unexpected error after {elapsed}s: {e}", exc_info=True)
        return jsonify({"error": "Server error. Try again later."}), 500


@app.route("/api/health", methods=["GET"])
def health_check():
    logged_in = session_available
    proxies_available = proxy_manager.available_count
    proxies_total = proxy_manager.total_count

    status = "ok" if proxies_available > 0 else "degraded"
    logger.info(f"[Health] logged_in={logged_in} proxies={proxies_available}/{proxies_total}")

    return jsonify({
        "status": status,
        "logged_in": logged_in,
        "session_user": os.environ.get("INSTAGRAM_USERNAME", "not set"),
        "proxies": {
            "available": proxies_available,
            "total": proxies_total,
            "mode": "gateway" if os.environ.get("PROXY_GATEWAY") else
                    "pool" if os.environ.get("PROXY_LIST") else
                    "direct (no proxy)"
        }
    })


@app.route("/api/proxy-status", methods=["GET"])
def proxy_status():
    """Debug endpoint — shows proxy health without exposing credentials."""
    with proxy_manager._lock:
        statuses = []
        for i, proxy in enumerate(proxy_manager._proxies):
            blacklist_expiry = proxy_manager._blacklisted.get(proxy)
            statuses.append({
                "index": i,
                "proxy": proxy_manager._mask(proxy),
                "failures": proxy_manager._failures.get(proxy, 0),
                "blacklisted": blacklist_expiry is not None and time.time() < blacklist_expiry,
                "recovers_in": (
                    round(blacklist_expiry - time.time())
                    if blacklist_expiry and time.time() < blacklist_expiry
                    else None
                )
            })

    return jsonify({
        "total": proxy_manager.total_count,
        "available": proxy_manager.available_count,
        "blacklist_ttl_seconds": proxy_manager._blacklist_ttl,
        "proxies": statuses
    })


if __name__ == "__main__":
    logger.info("Starting Flask server on 0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)