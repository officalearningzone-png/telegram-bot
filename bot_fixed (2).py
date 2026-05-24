import re
import time
import json
import random
import logging
import asyncio
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8989968199:AAGei3GSOUDef0AHfzDRFfH9KImHvoG9sCo"

(MAIN_MENU, PICK_CITY, PICK_RESTAURANT, RESULTS, PICK_BUDGET, PICK_SPLIT, PICK_PINCODE) = range(7)

_CACHE: dict = {}
CACHE_TTL = 900        # 15 min cache — Swiggy requests 33% kam honge
_CITY_LOCKS: dict = {}
_LOCKS_META = threading.Lock()

# ── HIGH-PERFORMANCE: Global shared thread pool ──────────────────────────────
# 3000 users handle karne ke liye — but Swiggy pe concurrent calls limit karo
_FETCH_POOL = ThreadPoolExecutor(max_workers=200, thread_name_prefix="swiggy")
# 200 workers enough hain — 500 se Swiggy ban karta hai + OS threads exhaust

_SWIGGY_SEM: asyncio.Semaphore | None = None

def _get_swiggy_sem() -> asyncio.Semaphore:
    global _SWIGGY_SEM
    if _SWIGGY_SEM is None:
        _SWIGGY_SEM = asyncio.Semaphore(80)  # 80 concurrent — safe threshold
    return _SWIGGY_SEM

# ── Per-user rate limiter ─────────────────────────────────────────────────────
_USER_LAST_REQUEST: dict = {}
_USER_COOLDOWN = 2.0  # 2 sec cooldown — reduce Swiggy hammering

def _check_rate_limit(user_id: int) -> bool:
    now = time.time()
    last = _USER_LAST_REQUEST.get(user_id, 0)
    if now - last < _USER_COOLDOWN:
        return False
    _USER_LAST_REQUEST[user_id] = now
    return True

# ══════════════════════════════════════════════════════════════════════════════
#  IP ROTATION + ANTI-BAN SYSTEM
#  3000 users → Swiggy single IP pe rate limit lagata hai
#  Solution: Multiple User-Agents + Request fingerprint rotation
#  (Free solution — paid proxies nahi chahiye)
# ══════════════════════════════════════════════════════════════════════════════

# Real mobile User-Agents pool — Swiggy bot detect karna mushkil ho jaata hai
_UA_POOL = [
    # Android Chrome (most common in India)
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.143 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.193 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.101 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; OnePlus 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.194 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Redmi Note 10 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.111 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Galaxy A54) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.144 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Realme GT 2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Mi 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.5938.140 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; iQOO 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.178 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; POCO F4 GT) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.163 Mobile Safari/537.36",
    # iPhone Safari (Swiggy supports iOS too)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/121.0.6167.138 Mobile/15E148 Safari/604.1",
    # Desktop Chrome (some users use desktop Swiggy)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

_ACCEPT_LANGUAGE_POOL = [
    "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-IN,hi;q=0.9,en;q=0.8",
    "hi-IN,hi;q=0.9,en-IN;q=0.8,en;q=0.7",
    "en-IN,en;q=0.9",
    "en-GB,en-IN;q=0.9,en-US;q=0.8,en;q=0.7",
]

_SWIGGY_COOKIE_BASE = "deviceId=sw_{uid}; citySlug=mumbai; userLocation=%7B%22lat%22%3A{lat}%2C%22lng%22%3A{lng}%7D"

# Request counter for rotating fingerprints
_REQ_COUNTER = 0
_REQ_LOCK = threading.Lock()

def _get_rotated_headers(lat: float = 19.07, lng: float = 72.87) -> dict:
    """Har request ke liye alag fingerprint — IP ban se bachao."""
    global _REQ_COUNTER
    with _REQ_LOCK:
        _REQ_COUNTER += 1
        idx = _REQ_COUNTER

    ua   = _UA_POOL[idx % len(_UA_POOL)]
    lang = _ACCEPT_LANGUAGE_POOL[idx % len(_ACCEPT_LANGUAGE_POOL)]
    uid  = f"{(idx * 7919) % 999999:06d}"  # pseudo-unique device ID

    is_mobile = "Mobile" in ua or "iPhone" in ua
    sec_ch_ua = '"Chromium";v="121", "Not A(Brand";v="99", "Google Chrome";v="121"'

    headers = {
        "User-Agent":       ua,
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  lang,
        "Accept-Encoding":  "gzip, deflate, br",
        "Origin":           "https://www.swiggy.com",
        "Referer":          f"https://www.swiggy.com/restaurants?lat={lat}&lng={lng}",
        "sec-fetch-dest":   "empty",
        "sec-fetch-mode":   "cors",
        "sec-fetch-site":   "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }
    if "Chrome" in ua and "iPhone" not in ua:
        headers["sec-ch-ua"]          = sec_ch_ua
        headers["sec-ch-ua-mobile"]   = "?1" if is_mobile else "?0"
        headers["sec-ch-ua-platform"] = '"Android"' if is_mobile else '"Windows"'

    # Add random small delay jitter to look human
    if idx % 5 == 0:
        time.sleep(random.uniform(0.05, 0.15))

    return headers

# Legacy SWIGGY_HEADERS — backward compat ke liye rakha
SWIGGY_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Linux; Android 12; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin":          "https://www.swiggy.com",
    "Referer":         "https://www.swiggy.com/",
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "same-origin",
}

# ── Smart Session Pool — Swiggy IP ban se bachne ke liye ─────────────────────
# Multiple sessions with different headers = looks like different users
_SESSION_POOL: list = []
_SESSION_POOL_SIZE = 20   # 20 different session fingerprints
_SESSION_IDX = 0
_SESSION_IDX_LOCK = threading.Lock()

def _build_session(idx: int) -> requests.Session:
    """Create one distinct session with unique fingerprint."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],  # 429 = rate limit
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=retry
    )
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    ua = _UA_POOL[idx % len(_UA_POOL)]
    s.headers.update({
        "User-Agent":      ua,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": _ACCEPT_LANGUAGE_POOL[idx % len(_ACCEPT_LANGUAGE_POOL)],
        "Accept-Encoding": "gzip, deflate, br",
        "Origin":          "https://www.swiggy.com",
        "Referer":         "https://www.swiggy.com/",
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    })
    return s

def _init_session_pool():
    """Bot start pe session pool bana lo."""
    global _SESSION_POOL
    _SESSION_POOL = [_build_session(i) for i in range(_SESSION_POOL_SIZE)]
    logger.info(f"✅ Session pool ready: {_SESSION_POOL_SIZE} fingerprints")

def _get_session() -> requests.Session:
    """Round-robin se alag-alag session deta hai — IP rotation simulate karta hai."""
    global _SESSION_IDX
    if not _SESSION_POOL:
        # Fallback: single session
        return _build_session(0)
    with _SESSION_IDX_LOCK:
        idx = _SESSION_IDX % _SESSION_POOL_SIZE
        _SESSION_IDX += 1
    return _SESSION_POOL[idx]

def _apply_session_headers():
    pass  # No-op — session pool already has headers

# ── IP Ban Detection + Auto-Recovery ─────────────────────────────────────────
_BAN_DETECTED = threading.Event()
_BAN_RECOVERY_TS = 0.0
_BAN_COOLDOWN = 60  # 60 sec wait when ban detected

def _check_ban_response(r: requests.Response) -> bool:
    """True = banned/rate limited."""
    if r.status_code == 429:
        return True
    if r.status_code in (403, 503):
        ct = r.headers.get("Content-Type", "")
        if "json" not in ct:
            return True  # HTML error page = likely Cloudflare block
    if r.status_code == 200:
        try:
            j = r.json()
            # Swiggy returns statusCode 1 on errors
            if j.get("statusCode") == 1 and "restaurants" not in str(j):
                return True
        except Exception:
            pass
    return False

def _safe_get(url: str, timeout: int = 12, lat: float = 0, lng: float = 0) -> requests.Response | None:
    """Rate-limit aware GET — ban detect kare toh wait karo."""
    global _BAN_RECOVERY_TS

    # Agar ban mode mein hain, wait karo
    now = time.time()
    if _BAN_DETECTED.is_set():
        if now - _BAN_RECOVERY_TS < _BAN_COOLDOWN:
            wait = _BAN_COOLDOWN - (now - _BAN_RECOVERY_TS)
            logger.warning(f"⏸️ Ban cooldown: {wait:.0f}s remaining")
            time.sleep(min(wait, 5))  # max 5 sec wait per call
            return None
        else:
            _BAN_DETECTED.clear()
            logger.info("✅ Ban cooldown over — resuming")
            # Rebuild session pool with fresh fingerprints
            _init_session_pool()

    sess = _get_session()
    # Override referer with actual lat/lng for more authentic requests
    if lat and lng:
        sess.headers["Referer"] = f"https://www.swiggy.com/restaurants?lat={lat}&lng={lng}"

    try:
        r = sess.get(url, timeout=timeout)
        if _check_ban_response(r):
            _BAN_DETECTED.set()
            _BAN_RECOVERY_TS = time.time()
            logger.warning(f"🚫 Ban/Rate-limit detected (status={r.status_code}) — cooling down {_BAN_COOLDOWN}s")
            return None
        return r
    except requests.exceptions.ConnectionError:
        logger.debug(f"Connection error: {url[:60]}")
        return None
    except requests.exceptions.Timeout:
        logger.debug(f"Timeout: {url[:60]}")
        return None
    except Exception as e:
        logger.debug(f"_safe_get error: {e}")
        return None

# ── Admin & Channel Settings ────────────────────────────────────────────────
ADMIN_ID = 8951202322
FORCE_CHANNELS = [
    {"username": "@Sheinxupdatelive", "name": "Sany x Update"},
]
_USERS: dict     = {}
_VERIFIED: set   = set()
_BOT_START_TS    = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  CITY MAP  — each city has a list of (lat, lng) scan points spread across
#  the metro area.  Big cities get 20-30 points; small ones get 8-12.
# ══════════════════════════════════════════════════════════════════════════════

CITIES: dict = {
    "delhi": {
        "display": "Delhi",
        "points": [
            (28.6139,77.2090),(28.5355,77.2090),(28.5733,77.2194),(28.6448,77.2167),
            (28.6562,77.2410),(28.6808,77.2673),(28.7041,77.1025),(28.6517,77.1309),
            (28.6280,77.3649),(28.6037,77.3609),(28.5244,77.1855),(28.5672,77.1851),
            (28.4836,77.0929),(28.7162,77.2085),(28.5921,77.0460),(28.5831,77.3191),
            (28.5460,77.2600),(28.6360,77.1750),(28.7496,77.1177),(28.6692,77.4538),
            (28.6100,77.3500),(28.5200,77.3000),(28.7300,77.1800),(28.6800,77.3200),
            (28.5500,77.1500),(28.7000,77.2500),(28.5800,77.2800),(28.6600,77.0800),
            (28.5000,77.2000),(28.7200,77.1600),
        ],
    },
    "new delhi": {"display": "Delhi", "ref": "delhi"},
    "mumbai": {
        "display": "Mumbai",
        "points": [
            (19.0760,72.8777),(19.0330,72.8654),(18.9388,72.8354),(19.1136,72.8697),
            (19.1764,72.9477),(18.9750,72.8258),(19.0596,72.8295),(18.9256,72.8242),
            (19.1397,72.9100),(19.0546,72.9315),(18.9969,72.8370),(19.2183,72.9781),
            (18.9600,72.8200),(19.1200,72.8500),(19.0800,72.8900),(18.9100,72.8100),
            (19.2000,72.9400),(19.0400,72.9000),(18.9800,72.8800),(19.1500,72.9300),
        ],
    },
    "bombay": {"display": "Mumbai", "ref": "mumbai"},
    "bangalore": {
        "display": "Bangalore",
        "points": [
            (12.9716,77.5946),(12.9352,77.6245),(12.9279,77.5784),(13.0358,77.5970),
            (12.9762,77.5163),(12.8456,77.6624),(13.0604,77.5473),(12.9141,77.6395),
            (12.9698,77.7499),(12.9260,77.4997),(12.9650,77.5000),(12.9100,77.5500),
            (13.0200,77.6200),(12.9500,77.6800),(13.0800,77.5200),(12.8800,77.6100),
            (12.9900,77.7000),(12.8600,77.5800),(13.0500,77.6600),(12.9300,77.5100),
        ],
    },
    "bengaluru": {"display": "Bangalore", "ref": "bangalore"},
    "hyderabad": {
        "display": "Hyderabad",
        "points": [
            (17.3850,78.4867),(17.4401,78.4800),(17.3616,78.4747),(17.4239,78.5601),
            (17.4947,78.3996),(17.3850,78.5618),(17.4600,78.3600),(17.3200,78.5200),
            (17.4000,78.4200),(17.5100,78.4400),(17.3500,78.5800),(17.4800,78.5000),
        ],
    },
    "chennai": {
        "display": "Chennai",
        "points": [
            (13.0827,80.2707),(12.9698,80.2408),(13.1067,80.2996),(13.0569,80.1936),
            (13.1479,80.2378),(13.0100,80.2100),(13.0600,80.2300),(12.9400,80.1800),
            (13.1800,80.2700),(13.0300,80.1500),(13.1200,80.2000),(12.9800,80.2900),
        ],
    },
    "madras": {"display": "Chennai", "ref": "chennai"},
    "kolkata": {
        "display": "Kolkata",
        "points": [
            (22.5726,88.3639),(22.5177,88.3581),(22.5958,88.2636),(22.6514,88.4341),
            (22.4974,88.3628),(22.5726,88.4495),(22.5200,88.3100),(22.6200,88.4100),
            (22.4700,88.3400),(22.6000,88.3800),(22.5500,88.2800),(22.6500,88.3300),
        ],
    },
    "calcutta": {"display": "Kolkata", "ref": "kolkata"},
    "pune": {
        "display": "Pune",
        "points": [
            (18.5204,73.8567),(18.5523,73.9143),(18.5314,73.8480),(18.4529,73.8496),
            (18.5679,73.7143),(18.4638,73.8687),(18.5900,73.9200),(18.5000,73.8000),
            (18.4300,73.9000),(18.6000,73.8000),(18.5200,73.7600),(18.4800,73.9300),
        ],
    },
    "ahmedabad": {
        "display": "Ahmedabad",
        "points": [
            (23.0225,72.5714),(23.0469,72.5340),(22.9977,72.5969),(23.0733,72.5114),
            (23.0000,72.6500),(22.9600,72.5300),(23.1000,72.5800),(22.9900,72.6000),
            (23.0500,72.5000),(22.9400,72.5600),(23.1100,72.5200),(23.0700,72.6200),
        ],
    },
    "noida": {
        "display": "Noida",
        "points": [
            (28.5355,77.3910),(28.5700,77.3210),(28.5200,77.4000),(28.5900,77.3600),
            (28.4744,77.5040),(28.5500,77.3400),(28.5100,77.4300),(28.6000,77.4000),
        ],
    },
    "gurgaon": {
        "display": "Gurgaon",
        "points": [
            (28.4595,77.0266),(28.4830,77.0890),(28.4207,77.0213),(28.5020,77.0500),
            (28.4300,77.0700),(28.5200,77.1000),(28.4700,77.0100),(28.4100,77.0500),
        ],
    },
    "gurugram": {"display": "Gurgaon", "ref": "gurgaon"},
    "faridabad": {
        "display": "Faridabad",
        "points": [
            (28.4089,77.3178),(28.4400,77.3500),(28.3800,77.2900),(28.3600,77.3200),(28.4600,77.2800),
        ],
    },
    "ghaziabad": {
        "display": "Ghaziabad",
        "points": [
            (28.6692,77.4538),(28.6900,77.4200),(28.6400,77.4900),(28.7200,77.4600),(28.6100,77.4100),
        ],
    },
    "chandigarh": {
        "display": "Chandigarh",
        "points": [
            (30.7333,76.7794),(30.7550,76.8100),(30.7050,76.7500),(30.7700,76.7600),(30.7200,76.8200),
        ],
    },
    "amritsar": {
        "display": "Amritsar",
        "points": [
            (31.6340,74.8723),(31.6500,74.9000),(31.6200,74.8500),(31.6700,74.8400),(31.6100,74.9200),
        ],
    },
    "ludhiana": {
        "display": "Ludhiana",
        "points": [
            (30.9010,75.8573),(30.9300,75.8800),(30.8700,75.8300),(30.9500,75.8200),(30.8500,75.8700),
        ],
    },
    "jalandhar": {
        "display": "Jalandhar",
        "points": [(31.3260,75.5762),(31.3500,75.6000),(31.3000,75.5500),(31.3700,75.5400)],
    },
    "jaipur": {
        "display": "Jaipur",
        "points": [
            (26.9124,75.7873),(26.8800,75.8000),(26.9400,75.7500),(26.9000,75.8200),
            (26.8500,75.7800),(26.9700,75.7700),(26.8300,75.8100),(26.9200,75.7200),
        ],
    },
    "jodhpur": {
        "display": "Jodhpur",
        "points": [(26.2389,73.0243),(26.2700,73.0500),(26.2100,73.0000),(26.2500,72.9900)],
    },
    "udaipur": {
        "display": "Udaipur",
        "points": [(24.5854,73.7125),(24.6100,73.7300),(24.5600,73.6900),(24.5500,73.7400)],
    },
    "kota": {
        "display": "Kota",
        "points": [(25.2138,75.8648),(25.2400,75.8900),(25.1900,75.8400),(25.1800,75.8800)],
    },
    "surat": {
        "display": "Surat",
        "points": [
            (21.1702,72.8311),(21.2000,72.8500),(21.1400,72.8100),(21.1900,72.7900),
            (21.2200,72.8700),(21.1100,72.8400),(21.1600,72.8600),(21.2100,72.8200),
        ],
    },
    "vadodara": {
        "display": "Vadodara",
        "points": [
            (22.3072,73.1812),(22.3400,73.2000),(22.2800,73.1600),(22.3000,73.2200),(22.3600,73.1500),
        ],
    },
    "baroda": {"display": "Vadodara", "ref": "vadodara"},
    "rajkot": {
        "display": "Rajkot",
        "points": [(22.3039,70.8022),(22.3300,70.8300),(22.2800,70.7800),(22.3200,70.7600)],
    },
    "nashik": {
        "display": "Nashik",
        "points": [(19.9975,73.7898),(20.0200,73.8100),(19.9700,73.7700),(20.0000,73.7400)],
    },
    "nagpur": {
        "display": "Nagpur",
        "points": [
            (21.1458,79.0882),(21.1700,79.1100),(21.1200,79.0600),(21.1800,79.0500),(21.1000,79.1200),
        ],
    },
    "aurangabad": {
        "display": "Aurangabad",
        "points": [(19.8762,75.3433),(19.9000,75.3700),(19.8500,75.3100),(19.8900,75.3900)],
    },
    "navi mumbai": {
        "display": "Navi Mumbai",
        "points": [(19.0330,73.0297),(19.0600,73.0100),(19.0100,73.0500),(19.0800,73.0400)],
    },
    "thane": {
        "display": "Thane",
        "points": [
            (19.2183,72.9781),(19.2400,72.9900),(19.1900,72.9600),(19.2600,72.9600),(19.1700,73.0000),
        ],
    },
    "bhopal": {
        "display": "Bhopal",
        "points": [
            (23.2599,77.4126),(23.2900,77.4300),(23.2300,77.3900),(23.2700,77.3700),(23.2000,77.4500),
        ],
    },
    "indore": {
        "display": "Indore",
        "points": [
            (22.7196,75.8577),(22.7500,75.8800),(22.6900,75.8300),(22.7400,75.8200),(22.6800,75.8800),
        ],
    },
    "lucknow": {
        "display": "Lucknow",
        "points": [
            (26.8467,80.9462),(26.8700,80.9700),(26.8200,80.9200),(26.8900,80.9100),(26.8100,80.9800),
        ],
    },
    "kanpur": {
        "display": "Kanpur",
        "points": [(26.4499,80.3319),(26.4700,80.3500),(26.4300,80.3100),(26.4600,80.3700)],
    },
    "prayagraj": {
        "display": "Prayagraj",
        "points": [(25.4358,81.8463),(25.4600,81.8700),(25.4100,81.8200),(25.4500,81.8100)],
    },
    "allahabad": {"display": "Prayagraj", "ref": "prayagraj"},
    "varanasi": {
        "display": "Varanasi",
        "points": [(25.3176,82.9739),(25.3400,83.0000),(25.2900,82.9500),(25.3300,82.9300)],
    },
    "banaras": {"display": "Varanasi", "ref": "varanasi"},
    "agra": {
        "display": "Agra",
        "points": [(27.1767,78.0081),(27.2000,78.0300),(27.1500,77.9800),(27.1900,78.0500)],
    },
    "meerut": {
        "display": "Meerut",
        "points": [(28.9845,77.7064),(29.0100,77.7300),(28.9600,77.6800),(29.0000,77.6600)],
    },
    "patna": {
        "display": "Patna",
        "points": [(25.5941,85.1376),(25.6200,85.1600),(25.5700,85.1100),(25.6000,85.0900)],
    },
    "ranchi": {
        "display": "Ranchi",
        "points": [(23.3441,85.3096),(23.3700,85.3300),(23.3200,85.2900),(23.3600,85.2700)],
    },
    "jamshedpur": {
        "display": "Jamshedpur",
        "points": [(22.8046,86.2029),(22.8300,86.2200),(22.7800,86.1800),(22.8100,86.2400)],
    },
    "bhubaneswar": {
        "display": "Bhubaneswar",
        "points": [(20.2961,85.8245),(20.3200,85.8400),(20.2700,85.8000),(20.3000,85.7800)],
    },
    "visakhapatnam": {
        "display": "Visakhapatnam",
        "points": [
            (17.6868,83.2185),(17.7100,83.2400),(17.6600,83.2000),(17.7300,83.1900),(17.6400,83.2600),
        ],
    },
    "vizag": {"display": "Visakhapatnam", "ref": "visakhapatnam"},
    "vijayawada": {
        "display": "Vijayawada",
        "points": [(16.5062,80.6480),(16.5300,80.6700),(16.4800,80.6200),(16.5200,80.6100)],
    },
    "coimbatore": {
        "display": "Coimbatore",
        "points": [
            (11.0168,76.9558),(11.0400,76.9800),(10.9900,76.9300),(11.0600,76.9200),(10.9700,76.9700),
        ],
    },
    "madurai": {
        "display": "Madurai",
        "points": [(9.9252,78.1198),(9.9500,78.1400),(9.9000,78.0900),(9.9700,78.0700)],
    },
    "kochi": {
        "display": "Kochi",
        "points": [
            (9.9312,76.2673),(9.9600,76.2900),(9.9000,76.2400),(9.9800,76.2200),(9.8900,76.2800),
        ],
    },
    "ernakulam": {"display": "Kochi", "ref": "kochi"},
    "thiruvananthapuram": {
        "display": "Thiruvananthapuram",
        "points": [(8.5241,76.9366),(8.5500,76.9600),(8.5000,76.9100),(8.5700,76.9000)],
    },
    "trivandrum": {"display": "Thiruvananthapuram", "ref": "thiruvananthapuram"},
    "kozhikode": {
        "display": "Kozhikode",
        "points": [(11.2588,75.7804),(11.2800,75.8000),(11.2300,75.7600),(11.2700,75.7400)],
    },
    "mysuru": {
        "display": "Mysuru",
        "points": [(12.2958,76.6394),(12.3200,76.6600),(12.2700,76.6100),(12.3100,76.6900)],
    },
    "mysore": {"display": "Mysuru", "ref": "mysuru"},
    "mangalore": {
        "display": "Mangalore",
        "points": [(12.9141,74.8560),(12.9400,74.8800),(12.8900,74.8300),(12.9200,74.8100)],
    },
    "mangaluru": {"display": "Mangalore", "ref": "mangalore"},
    "hubli": {
        "display": "Hubli",
        "points": [(15.3647,75.1240),(15.3900,75.1500),(15.3400,75.1000),(15.3700,75.0800)],
    },
    "belgaum": {
        "display": "Belgaum",
        "points": [(15.8497,74.4977),(15.8700,74.5200),(15.8300,74.4700),(15.8600,74.4500)],
    },
    "belagavi": {"display": "Belgaum", "ref": "belgaum"},
    "panaji": {
        "display": "Panaji (Goa)",
        "points": [(15.4909,73.8278),(15.5100,73.8500),(15.4700,73.8100),(15.5300,73.8000)],
    },
    "panjim": {"display": "Panaji (Goa)", "ref": "panaji"},
    "margao": {
        "display": "Margao (Goa)",
        "points": [(15.2832,74.0194),(15.3000,74.0400),(15.2600,73.9900),(15.3100,74.0700)],
    },
    "guwahati": {
        "display": "Guwahati",
        "points": [
            (26.1445,91.7362),(26.1700,91.7600),(26.1200,91.7100),(26.1800,91.7000),(26.1100,91.7500),
        ],
    },
    "dehradun": {
        "display": "Dehradun",
        "points": [(30.3165,78.0322),(30.3400,78.0500),(30.2900,78.0100),(30.3600,78.0700)],
    },
    "chandigarh": {
        "display": "Chandigarh",
        "points": [(30.7333,76.7794),(30.7550,76.8100),(30.7050,76.7500),(30.7700,76.7600)],
    },
    "shillong": {
        "display": "Shillong",
        "points": [(25.5788,91.8933),(25.6000,91.9100),(25.5600,91.8700),(25.5400,91.9200)],
    },
    "haridwar": {
        "display": "Haridwar",
        "points": [(29.9457,78.1642),(29.9700,78.1900),(29.9200,78.1400),(29.9600,78.1100)],
    },
    "rishikesh": {
        "display": "Rishikesh",
        "points": [(30.0869,78.2676),(30.1100,78.2900),(30.0600,78.2400),(30.1300,78.2500)],
    },
    "jammu": {
        "display": "Jammu",
        "points": [(32.7266,74.8570),(32.7500,74.8800),(32.7000,74.8300),(32.7600,74.8100)],
    },
    "srinagar": {
        "display": "Srinagar",
        "points": [(34.0837,74.7973),(34.1100,74.8200),(34.0600,74.7700),(34.0400,74.8300)],
    },
    "pondicherry": {
        "display": "Pondicherry",
        "points": [(11.9416,79.8083),(11.9700,79.8300),(11.9100,79.7800),(11.9500,79.8500)],
    },
    "puducherry": {"display": "Pondicherry", "ref": "pondicherry"},
    "greater noida": {
        "display": "Greater Noida",
        "points": [(28.4744,77.5040),(28.5000,77.5200),(28.4500,77.4800),(28.5200,77.4700)],
    },
    "patiala": {
        "display": "Patiala",
        "points": [(30.3398,76.3869),(30.3600,76.4100),(30.3200,76.3600),(30.3700,76.3500)],
    },
    "mohali": {
        "display": "Mohali",
        "points": [(30.7046,76.7179),(30.7300,76.7400),(30.6800,76.6900),(30.7500,76.7000)],
    },
    "gorakhpur": {
        "display": "Gorakhpur",
        "points": [(26.7606,83.3732),(26.7800,83.3900),(26.7400,83.3500),(26.7700,83.3300)],
    },
    "bilaspur": {
        "display": "Bilaspur",
        "points": [(22.0797,82.1409),(22.1000,82.1600),(22.0600,82.1200),(22.0900,82.1700)],
    },
    "raipur": {
        "display": "Raipur",
        "points": [
            (21.2514,81.6296),(21.2800,81.6500),(21.2200,81.6100),(21.2700,81.5900),(21.2100,81.6600),
        ],
    },
    "muzaffarpur": {
        "display": "Muzaffarpur",
        "points": [(26.1197,85.3910),(26.1400,85.4100),(26.1000,85.3700),(26.1300,85.3500)],
    },
    "dhanbad": {
        "display": "Dhanbad",
        "points": [(23.7957,86.4304),(23.8200,86.4500),(23.7700,86.4100),(23.8000,86.4700)],
    },
    "durgapur": {
        "display": "Durgapur",
        "points": [(23.5204,87.3119),(23.5400,87.3300),(23.5000,87.2900),(23.5500,87.2700)],
    },
    "siliguri": {
        "display": "Siliguri",
        "points": [(26.7271,88.3953),(26.7500,88.4200),(26.7000,88.3700),(26.7600,88.3600)],
    },
    "howrah": {
        "display": "Howrah",
        "points": [(22.5958,88.2636),(22.6200,88.2800),(22.5700,88.2400),(22.6000,88.2200)],
    },
    "cuttack": {
        "display": "Cuttack",
        "points": [(20.4625,85.8830),(20.4900,85.9000),(20.4400,85.8600),(20.4700,85.8400)],
    },
    "rourkela": {
        "display": "Rourkela",
        "points": [(22.2604,84.8536),(22.2800,84.8700),(22.2400,84.8300),(22.2700,84.8100)],
    },
    "guntur": {
        "display": "Guntur",
        "points": [(16.3008,80.4428),(16.3200,80.4600),(16.2800,80.4200),(16.3100,80.4900)],
    },
    "tirupati": {
        "display": "Tirupati",
        "points": [(13.6288,79.4192),(13.6500,79.4400),(13.6100,79.4000),(13.6400,79.3800)],
    },
    "warangal": {
        "display": "Warangal",
        "points": [(17.9784,79.6000),(18.0000,79.6200),(17.9600,79.5800),(17.9900,79.5600)],
    },
    "trichy": {
        "display": "Tiruchirappalli",
        "points": [(10.7905,78.7047),(10.8100,78.7200),(10.7700,78.6800),(10.8200,78.6600)],
    },
    "salem": {
        "display": "Salem",
        "points": [(11.6643,78.1460),(11.6900,78.1700),(11.6400,78.1200),(11.6700,78.1900)],
    },
    "vellore": {
        "display": "Vellore",
        "points": [(12.9165,79.1325),(12.9400,79.1500),(12.8900,79.1100),(12.9200,79.0900)],
    },
    "tiruppur": {
        "display": "Tiruppur",
        "points": [(11.1085,77.3411),(11.1300,77.3600),(11.0900,77.3200),(11.1200,77.3800)],
    },
    "thrissur": {
        "display": "Thrissur",
        "points": [(10.5276,76.2144),(10.5500,76.2300),(10.5100,76.1900),(10.5400,76.2500)],
    },
    "kollam": {
        "display": "Kollam",
        "points": [(8.8932,76.6141),(8.9100,76.6300),(8.8700,76.5900),(8.9200,76.6500)],
    },
    "kannur": {
        "display": "Kannur",
        "points": [(11.8745,75.3704),(11.9000,75.3900),(11.8500,75.3500),(11.8800,75.3300)],
    },
    "udupi": {
        "display": "Udupi",
        "points": [(13.3409,74.7421),(13.3600,74.7600),(13.3200,74.7200),(13.3500,74.7800)],
    },
    "davanagere": {
        "display": "Davanagere",
        "points": [(14.4644,75.9218),(14.4900,75.9400),(14.4400,75.9000),(14.4700,75.9600)],
    },
    "rohtak": {
        "display": "Rohtak",
        "points": [(28.8955,76.6066),(28.9200,76.6300),(28.8700,76.5800),(28.9000,76.5600)],
    },
    "hisar": {
        "display": "Hisar",
        "points": [(29.1492,75.7217),(29.1700,75.7400),(29.1300,75.7000),(29.1600,75.6800)],
    },
    "panipat": {
        "display": "Panipat",
        "points": [(29.3909,76.9635),(29.4100,76.9800),(29.3700,76.9400),(29.4000,76.9200)],
    },
    "ambala": {
        "display": "Ambala",
        "points": [(30.3782,76.7767),(30.4000,76.8000),(30.3600,76.7500),(30.3900,76.7300)],
    },
    "shimla": {
        "display": "Shimla",
        "points": [(31.1048,77.1734),(31.1300,77.1900),(31.0800,77.1500),(31.1100,77.2100)],
    },
    "ajmer": {
        "display": "Ajmer",
        "points": [(26.4499,74.6399),(26.4700,74.6600),(26.4300,74.6100),(26.4600,74.6700)],
    },
    "bikaner": {
        "display": "Bikaner",
        "points": [(28.0229,73.3119),(28.0500,73.3400),(28.0000,73.2900),(28.0400,73.2700)],
    },
    "alwar": {
        "display": "Alwar",
        "points": [(27.5530,76.6346),(27.5700,76.6500),(27.5300,76.6100),(27.5600,76.6700)],
    },
    "bhavnagar": {
        "display": "Bhavnagar",
        "points": [(21.7645,72.1519),(21.7900,72.1700),(21.7400,72.1300),(21.7700,72.1100)],
    },
    "gandhinagar": {
        "display": "Gandhinagar",
        "points": [(23.2156,72.6369),(23.2400,72.6600),(23.1900,72.6100),(23.2300,72.6700)],
    },
    "solapur": {
        "display": "Solapur",
        "points": [(17.6599,75.9064),(17.6800,75.9300),(17.6400,75.8800),(17.6700,75.8600)],
    },
    "kolhapur": {
        "display": "Kolhapur",
        "points": [(16.7050,74.2433),(16.7300,74.2600),(16.6800,74.2200),(16.7100,74.2700)],
    },
    "jabalpur": {
        "display": "Jabalpur",
        "points": [(23.1815,79.9864),(23.2000,80.0100),(23.1600,79.9600),(23.2100,79.9400)],
    },
    "gwalior": {
        "display": "Gwalior",
        "points": [(26.2183,78.1828),(26.2400,78.2000),(26.1900,78.1600),(26.2200,78.2200)],
    },
    "ujjain": {
        "display": "Ujjain",
        "points": [(23.1765,75.7885),(23.2000,75.8100),(23.1500,75.7600),(23.1900,75.7400)],
    },
    "mathura": {
        "display": "Mathura",
        "points": [(27.4924,77.6737),(27.5100,77.6900),(27.4700,77.6500),(27.5000,77.7100)],
    },
    "bareilly": {
        "display": "Bareilly",
        "points": [(28.3670,79.4304),(28.3900,79.4500),(28.3500,79.4100),(28.3800,79.4700)],
    },
    "aligarh": {
        "display": "Aligarh",
        "points": [(27.8974,78.0880),(27.9200,78.1100),(27.8700,78.0700),(27.9000,78.0500)],
    },
    "moradabad": {
        "display": "Moradabad",
        "points": [(28.8386,78.7733),(28.8600,78.7900),(28.8200,78.7500),(28.8500,78.8100)],
    },
    "manali": {
        "display": "Manali",
        "points": [(32.2396,77.1887),(32.2600,77.2100),(32.2200,77.1700),(32.2500,77.1500)],
    },
    # ── Northeast India ────────────────────────────────────────────────────────
    "imphal": {
        "display": "Imphal",
        "points": [(24.8170,93.9368),(24.8400,93.9600),(24.7900,93.9100),(24.8300,93.9700)],
    },
    "agartala": {
        "display": "Agartala",
        "points": [(23.8315,91.2868),(23.8500,91.3000),(23.8100,91.2700),(23.8600,91.2600)],
    },
    "dimapur": {
        "display": "Dimapur",
        "points": [(25.9040,93.7273),(25.9300,93.7500),(25.8800,93.7000),(25.9200,93.6900)],
    },
    "aizawl": {
        "display": "Aizawl",
        "points": [(23.7271,92.7176),(23.7500,92.7400),(23.7000,92.7000),(23.7400,92.6800)],
    },
    "gangtok": {
        "display": "Gangtok",
        "points": [(27.3389,88.6065),(27.3600,88.6200),(27.3100,88.5900),(27.3700,88.5700)],
    },
    "dibrugarh": {
        "display": "Dibrugarh",
        "points": [(27.4728,94.9120),(27.5000,94.9300),(27.4500,94.8900),(27.4900,94.9500)],
    },
    "silchar": {
        "display": "Silchar",
        "points": [(24.8333,92.7789),(24.8600,92.8000),(24.8100,92.7500),(24.8500,92.7300)],
    },
    "jorhat": {
        "display": "Jorhat",
        "points": [(26.7509,94.2037),(26.7700,94.2200),(26.7300,94.1800),(26.7600,94.1600)],
    },
    "itanagar": {
        "display": "Itanagar",
        "points": [(27.0844,93.6053),(27.1000,93.6200),(27.0600,93.5800),(27.0900,93.6400)],
    },
    # ── Bihar ─────────────────────────────────────────────────────────────────
    "gaya": {
        "display": "Gaya",
        "points": [(24.7914,85.0002),(24.8100,85.0200),(24.7700,84.9800),(24.8000,84.9600)],
    },
    "bhagalpur": {
        "display": "Bhagalpur",
        "points": [(25.2425,86.9842),(25.2700,87.0100),(25.2200,86.9600),(25.2600,86.9400)],
    },
    # ── Jharkhand ─────────────────────────────────────────────────────────────
    "bokaro": {
        "display": "Bokaro",
        "points": [(23.6693,86.1511),(23.6900,86.1700),(23.6500,86.1300),(23.6800,86.1100)],
    },
    "hazaribagh": {
        "display": "Hazaribagh",
        "points": [(23.9925,85.3629),(24.0100,85.3800),(23.9700,85.3400),(24.0000,85.3200)],
    },
    # ── Uttar Pradesh extra ────────────────────────────────────────────────────
    "saharanpur": {
        "display": "Saharanpur",
        "points": [(29.9680,77.5552),(29.9900,77.5700),(29.9400,77.5300),(29.9800,77.5900)],
    },
    "jhansi": {
        "display": "Jhansi",
        "points": [(25.4484,78.5685),(25.4700,78.5900),(25.4200,78.5400),(25.4600,78.5200)],
    },
    "haldwani": {
        "display": "Haldwani",
        "points": [(29.2183,79.5130),(29.2400,79.5300),(29.1900,79.4900),(29.2300,79.5500)],
    },
    "sahibabad": {
        "display": "Sahibabad",
        "points": [(28.6805,77.3493),(28.7000,77.3700),(28.6600,77.3300),(28.6900,77.3100)],
    },
    # ── Rajasthan extra ───────────────────────────────────────────────────────
    "bharatpur": {
        "display": "Bharatpur",
        "points": [(27.2152,77.4941),(27.2400,77.5100),(27.1900,77.4700),(27.2300,77.4500)],
    },
    "sri_ganganagar": {
        "display": "Sri Ganganagar",
        "points": [(29.9094,73.8830),(29.9300,73.9000),(29.8900,73.8600),(29.9200,73.8400)],
    },
    "sikar": {
        "display": "Sikar",
        "points": [(27.6094,75.1399),(27.6300,75.1600),(27.5900,75.1200),(27.6200,75.1700)],
    },
    "pali": {
        "display": "Pali",
        "points": [(25.7711,73.3233),(25.7900,73.3400),(25.7500,73.3000),(25.7800,73.3600)],
    },
    # ── Gujarat extra ─────────────────────────────────────────────────────────
    "junagadh": {
        "display": "Junagadh",
        "points": [(21.5222,70.4579),(21.5500,70.4800),(21.5000,70.4300),(21.5400,70.4100)],
    },
    "anand": {
        "display": "Anand",
        "points": [(22.5645,72.9289),(22.5900,72.9500),(22.5400,72.9100),(22.5800,72.8900)],
    },
    "vapi": {
        "display": "Vapi",
        "points": [(20.3893,72.9106),(20.4100,72.9300),(20.3700,72.8900),(20.4000,72.8700)],
    },
    "bharuch": {
        "display": "Bharuch",
        "points": [(21.7051,72.9959),(21.7300,73.0100),(21.6800,72.9700),(21.7100,72.9500)],
    },
    "gandhidham": {
        "display": "Gandhidham",
        "points": [(23.0753,70.1337),(23.1000,70.1500),(23.0500,70.1100),(23.0900,70.1700)],
    },
    "morbi": {
        "display": "Morbi",
        "points": [(22.8173,70.8370),(22.8400,70.8600),(22.7900,70.8100),(22.8300,70.7900)],
    },
    "navsari": {
        "display": "Navsari",
        "points": [(20.9467,72.9520),(20.9700,72.9700),(20.9200,72.9300),(20.9600,72.9100)],
    },
    # ── Maharashtra extra ─────────────────────────────────────────────────────
    "amravati": {
        "display": "Amravati",
        "points": [(20.9374,77.7796),(20.9600,77.8000),(20.9100,77.7500),(20.9500,77.7300)],
    },
    "akola": {
        "display": "Akola",
        "points": [(20.7002,77.0082),(20.7200,77.0300),(20.6800,76.9800),(20.7100,76.9600)],
    },
    "latur": {
        "display": "Latur",
        "points": [(18.4088,76.5604),(18.4300,76.5800),(18.3900,76.5400),(18.4200,76.5200)],
    },
    "nanded": {
        "display": "Nanded",
        "points": [(19.1383,77.3210),(19.1600,77.3400),(19.1100,77.3000),(19.1500,77.2800)],
    },
    "jalgaon": {
        "display": "Jalgaon",
        "points": [(21.0077,75.5626),(21.0300,75.5800),(20.9800,75.5400),(21.0200,75.5200)],
    },
    "sangli": {
        "display": "Sangli",
        "points": [(16.8524,74.5815),(16.8700,74.6000),(16.8300,74.5600),(16.8600,74.5400)],
    },
    "ahmednagar": {
        "display": "Ahmednagar",
        "points": [(19.0952,74.7496),(19.1200,74.7700),(19.0700,74.7300),(19.1100,74.7100)],
    },
    # ── Karnataka extra ───────────────────────────────────────────────────────
    "tumkur": {
        "display": "Tumkur",
        "points": [(13.3379,77.1010),(13.3600,77.1200),(13.3100,77.0800),(13.3500,77.1400)],
    },
    "shivamogga": {
        "display": "Shivamogga",
        "points": [(13.9299,75.5681),(13.9500,75.5900),(13.9100,75.5400),(13.9400,75.5200)],
    },
    "raichur": {
        "display": "Raichur",
        "points": [(16.2120,77.3566),(16.2300,77.3700),(16.1900,77.3300),(16.2200,77.3900)],
    },
    "dharwad": {
        "display": "Dharwad",
        "points": [(15.4589,75.0078),(15.4800,75.0300),(15.4400,74.9900),(15.4700,74.9700)],
    },
    "bidar": {
        "display": "Bidar",
        "points": [(17.9104,77.5199),(17.9300,77.5400),(17.8900,77.5000),(17.9200,77.4800)],
    },
    # ── Andhra Pradesh extra ──────────────────────────────────────────────────
    "kurnool": {
        "display": "Kurnool",
        "points": [(15.8281,78.0373),(15.8500,78.0600),(15.8000,78.0100),(15.8400,77.9900)],
    },
    "anantapur": {
        "display": "Anantapur",
        "points": [(14.6819,77.6006),(14.7000,77.6200),(14.6600,77.5800),(14.6900,77.5600)],
    },
    "rajahmundry": {
        "display": "Rajahmundry",
        "points": [(17.0005,81.8040),(17.0200,81.8200),(16.9800,81.7800),(17.0100,81.8400)],
    },
    "kadapa": {
        "display": "Kadapa",
        "points": [(14.4673,78.8242),(14.4900,78.8400),(14.4400,78.8000),(14.4800,78.7800)],
    },
    "nellore": {
        "display": "Nellore",
        "points": [(14.4426,79.9865),(14.4600,80.0100),(14.4200,79.9700),(14.4500,79.9500)],
    },
    # ── Tamil Nadu extra ──────────────────────────────────────────────────────
    "thanjavur": {
        "display": "Thanjavur",
        "points": [(10.7870,79.1378),(10.8100,79.1600),(10.7700,79.1100),(10.8000,79.0900)],
    },
    "erode": {
        "display": "Erode",
        "points": [(11.3410,77.7172),(11.3600,77.7400),(11.3200,77.6900),(11.3500,77.7600)],
    },
    "thoothukudi": {
        "display": "Thoothukudi",
        "points": [(8.7642,78.1348),(8.7900,78.1600),(8.7400,78.1100),(8.7800,78.0900)],
    },
    "nagercoil": {
        "display": "Nagercoil",
        "points": [(8.1833,77.4119),(8.2100,77.4300),(8.1600,77.3900),(8.2000,77.3700)],
    },
    "hosur": {
        "display": "Hosur",
        "points": [(12.7409,77.8253),(12.7600,77.8400),(12.7200,77.8000),(12.7500,77.8600)],
    },
    "dindigul": {
        "display": "Dindigul",
        "points": [(10.3624,77.9695),(10.3800,77.9900),(10.3400,77.9500),(10.3700,77.9300)],
    },
    "kumbakonam": {
        "display": "Kumbakonam",
        "points": [(10.9602,79.3845),(10.9800,79.4000),(10.9400,79.3600),(10.9700,79.4200)],
    },
    # ── Kerala extra ──────────────────────────────────────────────────────────
    "palakkad": {
        "display": "Palakkad",
        "points": [(10.7867,76.6548),(10.8100,76.6700),(10.7600,76.6300),(10.8000,76.6900)],
    },
    "malappuram": {
        "display": "Malappuram",
        "points": [(11.0730,76.0740),(11.0900,76.0900),(11.0500,76.0500),(11.0800,76.1100)],
    },
    "alappuzha": {
        "display": "Alappuzha",
        "points": [(9.4981,76.3388),(9.5200,76.3600),(9.4700,76.3200),(9.5100,76.3000)],
    },
    # ── Odisha extra ──────────────────────────────────────────────────────────
    "berhampur": {
        "display": "Berhampur",
        "points": [(19.3149,84.7941),(19.3400,84.8100),(19.2900,84.7700),(19.3300,84.7500)],
    },
    "sambalpur": {
        "display": "Sambalpur",
        "points": [(21.4669,83.9756),(21.4900,84.0000),(21.4400,83.9500),(21.4800,83.9300)],
    },
    "balasore": {
        "display": "Balasore",
        "points": [(21.4942,86.9329),(21.5200,86.9500),(21.4700,86.9100),(21.5100,86.8900)],
    },
    # ── West Bengal extra ─────────────────────────────────────────────────────
    "bardhaman": {
        "display": "Bardhaman",
        "points": [(23.2324,87.8615),(23.2500,87.8800),(23.2100,87.8400),(23.2400,87.8200)],
    },
    "kharagpur": {
        "display": "Kharagpur",
        "points": [(22.3460,87.3220),(22.3700,87.3400),(22.3200,87.3000),(22.3600,87.2800)],
    },
    "malda": {
        "display": "Malda",
        "points": [(25.0108,88.1417),(25.0300,88.1600),(24.9900,88.1200),(25.0200,88.1800)],
    },
    "haldia": {
        "display": "Haldia",
        "points": [(22.0667,88.0697),(22.0900,88.0900),(22.0400,88.0500),(22.0800,88.0300)],
    },
    # ── Madhya Pradesh extra ──────────────────────────────────────────────────
    "sagar_mp": {
        "display": "Sagar",
        "points": [(23.8388,78.7378),(23.8600,78.7600),(23.8100,78.7200),(23.8500,78.7000)],
    },
    "rewa": {
        "display": "Rewa",
        "points": [(24.5362,81.2999),(24.5600,81.3200),(24.5100,81.2800),(24.5500,81.2600)],
    },
    "satna": {
        "display": "Satna",
        "points": [(24.6005,80.8322),(24.6200,80.8500),(24.5800,80.8100),(24.6100,80.8700)],
    },
    "bhind": {
        "display": "Bhind",
        "points": [(26.5585,78.7877),(26.5800,78.8100),(26.5300,78.7600),(26.5700,78.7400)],
    },
    # ── Chhattisgarh extra ────────────────────────────────────────────────────
    "durg": {
        "display": "Durg",
        "points": [(21.1904,81.2849),(21.2100,81.3000),(21.1700,81.2700),(21.2000,81.2500)],
    },
    "bhilai": {
        "display": "Bhilai",
        "points": [(21.2167,81.3667),(21.2400,81.3900),(21.1900,81.3400),(21.2300,81.3200)],
    },
    "korba": {
        "display": "Korba",
        "points": [(22.3595,82.7501),(22.3800,82.7700),(22.3300,82.7300),(22.3700,82.7100)],
    },
    # ── Haryana extra ─────────────────────────────────────────────────────────
    "karnal": {
        "display": "Karnal",
        "points": [(29.6857,76.9905),(29.7100,77.0100),(29.6600,76.9700),(29.7000,76.9500)],
    },
    "sonipat": {
        "display": "Sonipat",
        "points": [(28.9288,77.0141),(28.9500,77.0300),(28.9100,76.9900),(28.9400,76.9700)],
    },
    "yamunanagar": {
        "display": "Yamunanagar",
        "points": [(30.1290,77.2674),(30.1500,77.2900),(30.1100,77.2500),(30.1400,77.2300)],
    },
    "rewari": {
        "display": "Rewari",
        "points": [(28.1972,76.6205),(28.2200,76.6400),(28.1700,76.6000),(28.2100,76.5800)],
    },
    # ── Punjab extra ──────────────────────────────────────────────────────────
    "bathinda": {
        "display": "Bathinda",
        "points": [(30.2110,74.9455),(30.2300,74.9700),(30.1900,74.9200),(30.2200,74.9000)],
    },
    "pathankot": {
        "display": "Pathankot",
        "points": [(32.2643,75.6421),(32.2900,75.6600),(32.2400,75.6200),(32.2800,75.6000)],
    },
    "hoshiarpur": {
        "display": "Hoshiarpur",
        "points": [(31.5343,75.9118),(31.5600,75.9300),(31.5100,75.8900),(31.5500,75.8700)],
    },
    "moga": {
        "display": "Moga",
        "points": [(30.8157,75.1601),(30.8400,75.1800),(30.7900,75.1400),(30.8300,75.1200)],
    },
    # ── Himachal Pradesh extra ────────────────────────────────────────────────
    "dharamshala": {
        "display": "Dharamshala",
        "points": [(32.2190,76.3234),(32.2400,76.3400),(32.1900,76.3000),(32.2300,76.2800)],
    },
    "mandi": {
        "display": "Mandi",
        "points": [(31.7083,76.9318),(31.7300,76.9500),(31.6900,76.9100),(31.7200,76.8900)],
    },
    # ── Uttarakhand extra ─────────────────────────────────────────────────────
    "roorkee": {
        "display": "Roorkee",
        "points": [(29.8543,77.8880),(29.8800,77.9100),(29.8300,77.8700),(29.8700,77.8500)],
    },
    "nainital": {
        "display": "Nainital",
        "points": [(29.3803,79.4636),(29.4000,79.4800),(29.3600,79.4400),(29.3900,79.4200)],
    },
    "kashipur": {
        "display": "Kashipur",
        "points": [(29.2100,78.9600),(29.2300,78.9800),(29.1900,78.9400),(29.2200,78.9200)],
    },
    # ── J&K extra ─────────────────────────────────────────────────────────────
    "baramulla": {
        "display": "Baramulla",
        "points": [(34.1983,74.3432),(34.2200,74.3600),(34.1800,74.3200),(34.2100,74.3000)],
    },
    "udhampur": {
        "display": "Udhampur",
        "points": [(32.9161,75.1416),(32.9400,75.1600),(32.8900,75.1200),(32.9300,75.1000)],
    },
}

POPULAR_CITIES = [
    "Delhi", "Mumbai", "Bangalore", "Hyderabad",
    "Chennai", "Kolkata", "Pune", "Ahmedabad",
    "Jaipur", "Lucknow", "Noida", "Gurgaon",
    "Chandigarh", "Kochi", "Indore", "Nagpur",
    "Surat", "Ranchi", "Patna", "Bhopal",
    "Visakhapatnam", "Vijayawada", "Coimbatore", "Mysuru",
    "Bhubaneswar", "Guwahati", "Dehradun", "Jamshedpur",
]


# ══════════════════════════════════════════════════════════════════════════════
#  SWIGGY  — multi-location parallel fetch
# ══════════════════════════════════════════════════════════════════════════════

def _extract_next_offset(data: dict) -> str:
    """Swiggy API se next page offset nikalo."""
    try:
        pages = data.get("data", {}).get("pageDetails", {})
        offset = pages.get("nextOffset") or pages.get("pageOffset")
        if offset: return str(offset)
        # Try alternate path
        for card in data.get("data", {}).get("cards", []):
            pg = card.get("card", {}).get("card", {}).get("id", "")
            if pg == "pageDetails":
                d = card.get("card", {}).get("card", {}).get("pageDetails", {})
                return str(d.get("nextOffset", ""))
    except Exception:
        pass
    return ""


def _fetch_pages(base_url: str, max_pages: int = 4, label: str = "", pt=None) -> list:
    """Generic paginated fetch for any Swiggy listing URL."""
    results: list = []
    seen: set = set()
    url = base_url
    sess = _get_session()
    for page in range(max_pages):
        try:
            r = sess.get(url, timeout=12)
            if r.status_code != 200:
                break
            data = r.json()
            before = len(results)
            _walk(data, results, seen, pt)
            if len(results) == before:
                break
            nxt = _extract_next_offset(data)
            if not nxt:
                break
            url = base_url + f"&pageOffset={nxt}&isFiltered=true"
            time.sleep(0.1)
        except Exception as e:
            logger.debug(f"fetch_pages error [{label}] page {page}: {e}")
            break
    return results


def _fetch_one(lat: float, lng: float) -> list:
    """Regular listing — all restaurants near this point (up to 4 pages)."""
    base = (f"https://www.swiggy.com/dapi/restaurants/list/v5"
            f"?lat={lat}&lng={lng}&page_type=DESKTOP_WEB_LISTING")
    return _fetch_pages(base, max_pages=4, label=f"regular {lat},{lng}", pt=(lat,lng))


def _fetch_discounted(lat: float, lng: float) -> list:
    """Discount-filter listing — ONLY restaurants with active offers."""
    results: list = []
    seen: set = set()
    sess = _get_session()
    for variant in [
        f"https://www.swiggy.com/dapi/restaurants/list/v5?lat={lat}&lng={lng}&filters=DISCOUNT",
        f"https://www.swiggy.com/dapi/restaurants/list/v5?lat={lat}&lng={lng}&sortBy=DISCOUNT&page_type=DESKTOP_WEB_LISTING",
    ]:
        try:
            r = sess.get(variant, timeout=12)
            if r.status_code == 200:
                _walk(r.json(), results, seen, (lat,lng))
        except Exception as e:
            logger.debug(f"fetch_discounted error {lat},{lng}: {e}")
    return results


def _walk(obj, out: list, seen: set, pt=None):
    if isinstance(obj, dict):
        info = obj.get("info")
        if info and isinstance(info, dict) and info.get("name"):
            name = info["name"]
            if name not in seen:
                seen.add(name)
                parsed = _parse_info(info)
                if pt:
                    parsed["lat"], parsed["lng"] = pt
                out.append(parsed)
        for v in obj.values():
            _walk(v, out, seen, pt)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, out, seen, pt)


def _parse_info(info: dict) -> dict:
    # Collect ALL distinct discount objects (V3, V2, V1) — each may be a separate deal
    seen_headers: set = set()
    parsed_discs: list = []
    for key in ("aggregatedDiscountInfoV3", "aggregatedDiscountInfoV2", "aggregatedDiscountInfo"):
        val = info.get(key)
        if not (val and isinstance(val, dict)):
            continue
        hdr = str(val.get("header") or val.get("subHeader") or "").strip()
        if not hdr or hdr in seen_headers:
            continue
        seen_headers.add(hdr)
        parsed_discs.append(_parse_disc(val))

    if not parsed_discs:
        parsed_discs = [_parse_disc({})]

    # Best-scoring disc is primary; rest become extra_offers
    parsed_discs.sort(key=lambda x: -x["score"])
    primary = parsed_discs[0]
    extra_offers = [p for p in parsed_discs[1:] if p.get("offer")]

    sla  = info.get("sla", {})
    cost = str(info.get("costForTwo") or "")
    area = (info.get("areaName") or info.get("locality") or "")
    rest_id = str(info.get("id") or "")
    raw_name = info.get("name", "")
    slug = re.sub(r'[^a-z0-9]+', '-', raw_name.lower()).strip('-')
    return {
        "name":         raw_name,
        "rest_id":      rest_id,
        "slug":         slug,
        "cuisine":      ", ".join(info.get("cuisines", [])),
        "rating":       str(info.get("avgRating") or ""),
        "votes":        str(info.get("totalRatingsString") or ""),
        "time":         str(sla.get("deliveryTime") or sla.get("slaString") or ""),
        "cost":         cost,
        "cost_num":     _parse_cost(cost),
        "area":         area,
        "extra_offers": extra_offers,
        **primary,
    }


def _parse_disc(disc: dict) -> dict:
    header    = str(disc.get("header") or "").strip()
    subheader = str(disc.get("subHeader") or "").strip()
    coupon    = str(disc.get("couponCode") or "").strip()

    pct = flat = upto = min_order = 0

    # descriptionList mein discountType se directly extract karo (most reliable)
    desc_texts = []
    for item in (disc.get("descriptionList") or []):
        if not isinstance(item, dict): continue
        t     = str(item.get("meta") or item.get("text") or "").strip()
        dtype = str(item.get("discountType") or "").upper()
        if t:
            desc_texts.append(t)
        if dtype == "MINORDER" and not min_order:
            m = re.search(r'(\d+)', t.replace(",", ""))
            if m: min_order = int(m.group(1))
        elif dtype in ("MAXAMOUNT", "MAX_AMOUNT") and not upto:
            m = re.search(r'(\d+)', t.replace(",", ""))
            if m: upto = int(m.group(1))

    desc_combined = " | ".join(desc_texts)
    # Prefer descriptionList text (most complete — includes min_order, coupon hints)
    # Fall back to header+subheader when desc is empty
    if desc_combined:
        full = desc_combined
    else:
        full = " ".join(filter(None, [header, subheader])).strip()

    if full:
        m = re.search(r'(\d+)\s*%', full)
        if m: pct = int(m.group(1))

        if not upto:
            m = re.search(r'upto\s*(?:rs\.?\s*|₹\s*)(\d+)', full, re.I)
            if not m: m = re.search(r'(?:rs\.?\s*|₹\s*)(\d+)\s*(?:off)?(?:\s*max|\s*upto)', full, re.I)
            if m: upto = int(m.group(1))

        # flat only when there is NO percentage — pct and flat are mutually exclusive.
        # "60% OFF upto ₹166" must NOT set flat=166 (that would misrepresent the deal).
        if not pct:
            m = re.search(r'(?:flat|off)\s*(?:rs\.?\s*|₹\s*)(\d+)', full, re.I)
            if not m: m = re.search(r'(?:rs\.?\s*|₹\s*)(\d+)\s*(?:off|flat)', full, re.I)
            if m: flat = int(m.group(1))

        if not min_order:
            m = re.search(r'(?:above|on orders?(?: of| above)?|min(?:imum)?\.?|orders? of)\s*(?:rs\.?\s*|₹\s*)(\d+)', full, re.I)
            if not m: m = re.search(r'(?:rs\.?\s*|₹\s*)(\d+)\s*(?:and above|minimum|min|\+)', full, re.I)
            if not m: m = re.search(r'(\d{3,})\+', full)   # e.g. 149+
            if m: min_order = int(m.group(1))

    # Score = realistic max savings value
    if pct and upto:
        score = upto                     # max capped savings
    elif pct:
        score = pct * 3                  # rough proxy
    else:
        score = flat                     # true flat amount
    return {"offer": full, "coupon": coupon,
            "pct": pct, "flat": flat, "upto": upto,
            "min_order": min_order, "score": score}


def _parse_cost(s) -> int:
    if not s: return 0
    m = re.search(r'(\d+)', str(s).replace(',', ''))
    return int(m.group(1)) // 2 if m else 0


def _resolve_city_key(key: str) -> str:
    """Follow any 'ref' alias."""
    c = CITIES.get(key, {})
    return c.get("ref", key)


def _fetch_raw(city_key: str) -> list:
    key = _resolve_city_key(city_key)
    cache_key = key
    now = time.time()
    if cache_key in _CACHE and (now - _CACHE[cache_key]["ts"]) < CACHE_TTL:
        return _CACHE[cache_key]["data"]

    city_data = CITIES.get(key, {})
    points = city_data.get("points", [])
    if not points:
        logger.warning(f"No points for city key: {key}")
        return []

    all_restaurants: dict = {}

    def _merge(r_list):
        for r in r_list:
            name = r["name"]
            if name not in all_restaurants:
                all_restaurants[name] = r
            else:
                if r["score"] > all_restaurants[name]["score"]:
                    all_restaurants[name] = r

    # ── OPTIMIZED: Global pool use karo, nested pool nahi ────────────────────
    # Nested ThreadPoolExecutor creates new threads per fetch — thread explosion!
    # Global _FETCH_POOL sab users ke beech shared hai — efficient & bounded.
    futs = {}
    for lat, lng in points:
        futs[_FETCH_POOL.submit(_fetch_one, lat, lng)]        = "regular"
        futs[_FETCH_POOL.submit(_fetch_discounted, lat, lng)] = "discount"
    for fut in as_completed(futs):
        try:
            _merge(fut.result())
        except Exception as e:
            logger.debug(f"worker error: {e}")

    result = list(all_restaurants.values())
    logger.info(f"Fetched {len(result)} unique restaurants for {key}")
    _CACHE[cache_key] = {"ts": now, "data": result}
    return result




# ══════════════════════════════════════════════════════════════════════════════
#  REAL PER-RESTAURANT OFFERS — Swiggy menu/pl API gives the COMPLETE offer
#  list (bank offers, coupon codes, freebies) — listing endpoint only returns
#  the headline banner.  Use this when the user searches a specific restaurant.
# ══════════════════════════════════════════════════════════════════════════════

_REST_OFFER_CACHE: dict = {}
REST_OFFER_TTL = 300  # 5 minutes

def _fetch_restaurant_offers(rest_id: str, lat: float, lng: float) -> list:
    """Hit Swiggy menu/pl endpoint and extract ALL real offers for one restaurant.
    Returns a dict: {"offers": [...], "prices": {...}} with real menu prices."""
    if not rest_id:
        return {"offers": [], "prices": {}}
    ck = str(rest_id)
    now = time.time()
    if ck in _REST_OFFER_CACHE and (now - _REST_OFFER_CACHE[ck]["ts"]) < REST_OFFER_TTL:
        return _REST_OFFER_CACHE[ck]["data"]

    url = (f"https://www.swiggy.com/dapi/menu/pl?page-type=REGULAR_MENU"
           f"&complete-menu=true&lat={lat}&lng={lng}&restaurantId={rest_id}")
    offers: list = []
    seen_keys: set = set()
    items: list = []
    seen_items: set = set()
    try:
        sess = _get_session()
        r = sess.get(url, timeout=12)
        if r.status_code != 200:
            return {"offers": [], "prices": {}}
        data = r.json()

        def _walk_offers(obj):
            if isinstance(obj, dict):
                # Swiggy puts offers under cards[].card.card.gridElements.infoWithStyle.offers[].info
                if "offers" in obj and isinstance(obj["offers"], list):
                    for o in obj["offers"]:
                        info = (o.get("info") if isinstance(o, dict) else None) or o
                        if not isinstance(info, dict):
                            continue
                        header = str(info.get("header") or "").strip()
                        desc   = str(info.get("description") or "").strip()
                        coupon = str(info.get("couponCode") or "").strip()
                        otype  = str(info.get("offerType") or "").strip()
                        oltag  = str(info.get("offerLogo") or "").strip()
                        bottom = str(info.get("offerTagInfo", {}).get("offerTag") if isinstance(info.get("offerTagInfo"), dict) else (info.get("bottomDescription") or "")).strip()
                        # Some payloads put the real min-order / cashback bits in descriptionTextColor, validityMessage, etc.
                        validity = str(info.get("validityMessage") or info.get("validityDescription") or "").strip()
                        if not (header or desc or coupon):
                            continue
                        key = (header, desc, coupon)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        offers.append({
                            "header":      header,
                            "description": desc,
                            "coupon":      coupon,
                            "type":        otype,
                            "logo":        oltag,
                            "bottom":      bottom,
                            "validity":    validity,
                        })
                for v in obj.values():
                    _walk_offers(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk_offers(item)

        _walk_offers(data)

        # ── Walk menu item cards for REAL prices ──
        def _walk_items(obj):
            if isinstance(obj, dict):
                # Menu item shape: { "card": { "info": { "name": "...", "price": 24900, "defaultPrice": ... } } }
                info = obj.get("info") if isinstance(obj.get("info"), dict) else None
                if info and ("price" in info or "defaultPrice" in info) and info.get("name"):
                    raw = info.get("price") or info.get("defaultPrice") or 0
                    try:
                        price = int(raw) // 100 if raw and int(raw) >= 100 else int(raw or 0)
                    except Exception:
                        price = 0
                    nm = str(info.get("name") or "").strip()
                    if nm and price > 0 and nm.lower() not in seen_items:
                        seen_items.add(nm.lower())
                        items.append({"name": nm, "price": price})
                for v in obj.values():
                    _walk_items(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk_items(item)

        _walk_items(data)
    except Exception as e:
        logger.debug(f"fetch_restaurant_offers error rest={rest_id}: {e}")
        return {"offers": [], "prices": {}}

    # Build price summary
    prices: dict = {}
    if items:
        plist = sorted(items, key=lambda x: x["price"])
        # Filter out obvious junk (₹1 add-ons, etc.) for stats only — keep cheap items in list
        meaningful = [p for p in plist if p["price"] >= 30]
        stat_src = meaningful or plist
        prices = {
            "min":     stat_src[0]["price"],
            "max":     stat_src[-1]["price"],
            "avg":     sum(p["price"] for p in stat_src) // len(stat_src),
            "count":   len(items),
            "cheapest": plist[:3],   # 3 cheapest items (name + price)
        }

    payload = {"offers": offers, "prices": prices}
    _REST_OFFER_CACHE[ck] = {"ts": now, "data": payload}
    return payload


async def async_fetch_restaurant_offers(rest_id: str, lat: float, lng: float) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_FETCH_POOL, _fetch_restaurant_offers, rest_id, lat, lng)


def _format_real_offers(r: dict, payload, city_slug: str = "") -> str:
    """Render the full real offer list for one restaurant as Markdown."""
    # Back-compat: agar list aaye to wrap kar do
    if isinstance(payload, list):
        payload = {"offers": payload, "prices": {}}
    payload = payload or {"offers": [], "prices": {}}
    real_offers = payload.get("offers") or []
    prices      = payload.get("prices") or {}

    name = r.get("name", "Restaurant")
    area = r.get("area", "")
    rating = r.get("rating", "")
    url  = _rest_url(r, city_slug)
    name_part = f"[{name}]({url})" if url else f"*{name}*"
    head = [f"🍴 {name_part}"]
    sub = []
    if area:   sub.append(f"📍 {area}")
    if rating: sub.append(f"⭐ {rating}")
    if sub: head.append("  ·  ".join(sub))
    if url:
        head.append(f"🔗 {url}")
    head.append("━━━━━━━━━━━━━━━━")

    # ── REAL menu prices block ──
    if prices:
        head.append(
            f"💵 *Live Menu Prices:* ₹{prices['min']}–₹{prices['max']}"
            f"  ·  Avg ₹{prices['avg']}  ·  {prices['count']} items"
        )
        if prices.get("cheapest"):
            head.append("🥇 *Cheapest items:*")
            for it in prices["cheapest"]:
                nm = it["name"][:40]
                head.append(f"   • {nm} — *₹{it['price']}*")
        head.append("")

    if not real_offers:
        head.append("\n😕 Is restaurant pe abhi koi active offer Swiggy pe nahi mila.")
        return "\n".join(head)

    head.append(f"🎁 *{len(real_offers)} REAL OFFERS LIVE:*\n")
    for i, o in enumerate(real_offers, 1):
        title = o["header"] or o["description"] or "Offer"
        line  = f"*{i}.* 🔥 {title}"
        if o["description"] and o["description"] != o["header"]:
            line += f"\n     _{o['description']}_"
        if o.get("bottom") and o["bottom"] not in (o["header"], o["description"]):
            line += f"\n     💡 {o['bottom']}"
        if o.get("validity"):
            line += f"\n     ⏳ {o['validity']}"
        if o["coupon"]:
            line += f"\n     🎟️ Code: `{o['coupon']}`"
        head.append(line)
        head.append("")
    head.append(f"_Live from Swiggy · {_ts()}_")
    return "\n".join(head)


# ── Data filters ────────────────────────────────────────────────────────────

def _srt(lst):
    return sorted(lst, key=lambda r: -r["score"])


def get_all(city_key):    return _fetch_raw(city_key)
def get_offers(city_key): return _srt([r for r in get_all(city_key) if r["offer"]])
def get_coupons(city_key):return _srt([r for r in get_all(city_key) if r["coupon"]])
def get_top(city_key):    return get_offers(city_key)[:15]


async def _async_get_all(city_key: str) -> list:
    """Non-blocking fetch — event loop block nahi karta.
    Per-city lock: same city ka sirf ek fetch chalta hai at a time.
    Baaki users cached result milta hai jab fetch complete hota hai."""
    key = _resolve_city_key(city_key)

    # Fast path: cache hit — lock bhi nahi chahiye
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    # ── RACE-CONDITION FIX: Lock creation bhi protect karo ───────────────────
    # Pehle: multiple coroutines ek saath `if key not in _CITY_LOCKS` check
    # karthe the — alag-alag locks bante the!
    # Ab: asyncio.Lock() ek hi thread (event loop) mein chalta hai,
    # isliye simple dict check safe hai (no threading.Lock needed here).
    if key not in _CITY_LOCKS:
        _CITY_LOCKS[key] = asyncio.Lock()
    lock = _CITY_LOCKS[key]

    async with lock:
        # Double-check after acquiring lock — dusre waiter ne fetch kar liya hoga
        now = time.time()
        cached = _CACHE.get(key)
        if cached and (now - cached["ts"]) < CACHE_TTL:
            return cached["data"]

        # Blocking fetch ko shared thread pool mein run karo
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_FETCH_POOL, _fetch_raw, city_key)


# ── Static PIN → (lat, lng, area) fallback for common Indian cities ──────────
# Swiggy-active cities ke common PINs — API fail hone pe turant use hoga
_PIN_STATIC: dict = {
    # Delhi
    "110001":(28.6353,77.2249,"Connaught Place, New Delhi"),
    "110002":(28.6300,77.2400,"Darya Ganj, Delhi"),
    "110003":(28.6400,77.2200,"Civil Lines, Delhi"),
    "110005":(28.6519,77.1928,"Karol Bagh, Delhi"),
    "110006":(28.6583,77.2293,"Chandni Chowk, Delhi"),
    "110009":(28.6800,77.2100,"Model Town, Delhi"),
    "110011":(28.5933,77.1868,"Vasant Vihar, Delhi"),
    "110014":(28.5774,77.2028,"Lajpat Nagar, Delhi"),
    "110016":(28.5494,77.1921,"Hauz Khas, Delhi"),
    "110017":(28.5339,77.2178,"Malviya Nagar, Delhi"),
    "110019":(28.5458,77.2534,"Kalkaji, Delhi"),
    "110020":(28.5277,77.2558,"Okhla, Delhi"),
    "110025":(28.5586,77.1860,"Saket, Delhi"),
    "110048":(28.5700,77.2900,"Jasola, Delhi"),
    "110049":(28.5100,77.2800,"Tughlaqabad, Delhi"),
    "110051":(28.6700,77.3100,"Shahdara, Delhi"),
    "110059":(28.6258,77.0887,"Janakpuri, Delhi"),
    "110063":(28.6567,77.0912,"Paschim Vihar, Delhi"),
    "110075":(28.5843,77.0489,"Dwarka, Delhi"),
    "110085":(28.7071,77.1180,"Rohini, Delhi"),
    "110092":(28.6519,77.3061,"Patparganj, Delhi"),
    "110096":(28.6237,77.3753,"Mayur Vihar, Delhi"),
    # Mumbai
    "400001":(18.9322,72.8264,"Fort, Mumbai"),
    "400002":(18.9583,72.8364,"Mazgaon, Mumbai"),
    "400003":(18.9600,72.8200,"Byculla, Mumbai"),
    "400004":(18.9700,72.8100,"Girgaon, Mumbai"),
    "400005":(18.9000,72.8200,"Colaba, Mumbai"),
    "400006":(18.9600,72.8000,"Malabar Hill, Mumbai"),
    "400007":(18.9719,72.8241,"Matunga, Mumbai"),
    "400008":(19.0200,72.8400,"Dadar, Mumbai"),
    "400011":(19.0196,72.8553,"Parel, Mumbai"),
    "400013":(19.0261,72.8400,"Naigaon, Mumbai"),
    "400014":(19.0400,72.8600,"Mahim, Mumbai"),
    "400016":(19.0500,72.8300,"Worli, Mumbai"),
    "400018":(19.0100,72.8500,"Prabhadevi, Mumbai"),
    "400019":(19.0500,72.8700,"Sion, Mumbai"),
    "400022":(19.0572,72.8774,"Chunabhatti, Mumbai"),
    "400028":(19.0800,72.8900,"Matunga West, Mumbai"),
    "400029":(19.0700,72.8800,"Dharavi, Mumbai"),
    "400050":(19.1300,72.8300,"Bandra West, Mumbai"),
    "400051":(19.0600,72.8600,"Khar West, Mumbai"),
    "400052":(19.0800,72.8700,"Santa Cruz, Mumbai"),
    "400053":(19.1000,72.8500,"Vile Parle, Mumbai"),
    "400054":(19.1100,72.8400,"Andheri West, Mumbai"),
    "400055":(19.1200,72.8500,"Andheri East, Mumbai"),
    "400057":(19.0900,72.8900,"Kurla, Mumbai"),
    "400059":(19.1600,72.8300,"Goregaon, Mumbai"),
    "400060":(19.1800,72.8500,"Malad, Mumbai"),
    "400061":(19.1900,72.8400,"Kandivali, Mumbai"),
    "400064":(19.2200,72.8600,"Borivali, Mumbai"),
    "400068":(19.1300,72.9100,"Ghatkopar, Mumbai"),
    "400069":(19.1500,72.9200,"Vikhroli, Mumbai"),
    "400070":(19.0900,72.9100,"Chembur, Mumbai"),
    "400071":(19.1200,72.9300,"Powai, Mumbai"),
    "400072":(19.1600,72.9400,"Bhandup, Mumbai"),
    "400078":(19.1900,72.9600,"Mulund, Mumbai"),
    "400079":(19.1700,72.9600,"Nahur, Mumbai"),
    "400080":(19.1500,72.9000,"Kanjur Marg, Mumbai"),
    "400081":(19.0500,72.9200,"Mankhurd, Mumbai"),
    "400086":(19.2000,72.9800,"Thane West"),
    "400088":(19.2400,72.9900,"Thane East"),
    "400601":(19.0330,73.0297,"Navi Mumbai CBD"),
    "400614":(19.0600,73.0100,"Vashi, Navi Mumbai"),
    "400615":(19.0800,73.0000,"Kopar Khairane, Navi Mumbai"),
    "400703":(19.0100,73.0500,"Nerul, Navi Mumbai"),
    "400706":(18.9900,73.1200,"Panvel, Navi Mumbai"),
    # Bangalore
    "560001":(12.9716,77.5946,"MG Road, Bangalore"),
    "560002":(12.9776,77.5764,"Shivajinagar, Bangalore"),
    "560003":(12.9352,77.6245,"Koramangala, Bangalore"),
    "560004":(12.9652,77.5888,"Rajajinagar, Bangalore"),
    "560008":(12.9762,77.5163,"Vijayanagar, Bangalore"),
    "560010":(12.9698,77.7499,"Whitefield, Bangalore"),
    "560011":(13.0358,77.5970,"Yelahanka, Bangalore"),
    "560016":(12.9141,77.6395,"BTM Layout, Bangalore"),
    "560017":(12.9279,77.5784,"JP Nagar, Bangalore"),
    "560019":(12.9100,77.5500,"Banashankari, Bangalore"),
    "560020":(12.8900,77.5900,"Padmanabhanagar, Bangalore"),
    "560027":(12.9500,77.6800,"Indiranagar, Bangalore"),
    "560029":(12.9900,77.7000,"KR Puram, Bangalore"),
    "560034":(13.0604,77.5473,"Hebbal, Bangalore"),
    "560037":(12.9260,77.4997,"Kengeri, Bangalore"),
    "560038":(12.9650,77.5000,"Nagarbhavi, Bangalore"),
    "560041":(13.0800,77.5200,"Bagalur, Bangalore"),
    "560043":(13.0200,77.6200,"Banaswadi, Bangalore"),
    "560047":(12.8800,77.6100,"Electronic City, Bangalore"),
    "560048":(12.8600,77.5800,"Hulimavu, Bangalore"),
    "560068":(12.9300,77.5100,"Uttarahalli, Bangalore"),
    "560076":(12.9900,77.6500,"Marathahalli, Bangalore"),
    "560078":(13.0500,77.6600,"Horamavu, Bangalore"),
    "560085":(12.9200,77.6000,"Hongasandra, Bangalore"),
    "560100":(12.9716,77.5946,"Bangalore Central"),
    # Hyderabad
    "500001":(17.3850,78.4867,"Hyderabad"),
    "500003":(17.4001,78.4746,"Secunderabad"),
    "500004":(17.4239,78.5601,"Uppal, Hyderabad"),
    "500007":(17.3750,78.5300,"Malakpet, Hyderabad"),
    "500008":(17.3616,78.4747,"Banjara Hills, Hyderabad"),
    "500016":(17.4401,78.4800,"Malkajgiri, Hyderabad"),
    "500028":(17.4200,78.4500,"Begumpet, Hyderabad"),
    "500029":(17.4947,78.3996,"Kukatpally, Hyderabad"),
    "500032":(17.4400,78.3700,"HITEC City, Hyderabad"),
    "500033":(17.3850,78.5618,"LB Nagar, Hyderabad"),
    "500034":(17.4600,78.3600,"Madhapur, Hyderabad"),
    "500035":(17.5100,78.4400,"Kompally, Hyderabad"),
    "500039":(17.4100,78.5200,"Dilsukhnagar, Hyderabad"),
    "500048":(17.3500,78.5800,"Mehdipatnam, Hyderabad"),
    "500055":(17.4800,78.5000,"Maredpally, Secunderabad"),
    "500072":(17.3200,78.5200,"Attapur, Hyderabad"),
    "500081":(17.4900,78.4200,"Alwal, Hyderabad"),
    # Chennai
    "600001":(13.0827,80.2707,"Chennai Central"),
    "600002":(13.0900,80.2800,"Sowcarpet, Chennai"),
    "600006":(13.0569,80.1936,"Aminjikarai, Chennai"),
    "600010":(13.0600,80.2300,"Saidapet, Chennai"),
    "600017":(13.0300,80.1500,"Ashok Nagar, Chennai"),
    "600018":(13.0700,80.2000,"Kodambakkam, Chennai"),
    "600020":(13.1479,80.2378,"Perambur, Chennai"),
    "600024":(13.0100,80.2100,"Guindy, Chennai"),
    "600026":(12.9800,80.2900,"Adyar, Chennai"),
    "600028":(13.0561,80.2090,"Nandanam, Chennai"),
    "600035":(13.1800,80.2700,"Anna Nagar, Chennai"),
    "600040":(12.9400,80.1800,"Tambaram, Chennai"),
    "600041":(13.0500,80.2600,"Velachery, Chennai"),
    "600042":(13.0200,80.2500,"Alandur, Chennai"),
    "600045":(13.1100,80.1700,"Koyambedu, Chennai"),
    "600052":(13.1200,80.2000,"Kolathur, Chennai"),
    "600083":(12.9698,80.2408,"Sholinganallur, Chennai"),
    "600091":(13.1067,80.2996,"Tondiarpet, Chennai"),
    "600096":(13.1200,80.2200,"Villivakkam, Chennai"),
    # Kolkata
    "700001":(22.5726,88.3639,"Kolkata GPO"),
    "700005":(22.5200,88.3100,"Behala, Kolkata"),
    "700006":(22.5177,88.3581,"Alipore, Kolkata"),
    "700012":(22.5726,88.3800,"Shyambazar, Kolkata"),
    "700013":(22.5400,88.3500,"Ballygunge, Kolkata"),
    "700016":(22.5958,88.2636,"Kidderpore, Kolkata"),
    "700017":(22.5100,88.3600,"Regent Park, Kolkata"),
    "700019":(22.5300,88.3700,"Jadavpur, Kolkata"),
    "700020":(22.5100,88.3800,"Tollygunge, Kolkata"),
    "700025":(22.4974,88.3628,"Thakurpukur, Kolkata"),
    "700029":(22.5726,88.4495,"Maniktala, Kolkata"),
    "700032":(22.4700,88.3400,"Joka, Kolkata"),
    "700033":(22.5400,88.4100,"Gariahat, Kolkata"),
    "700040":(22.6200,88.4100,"Lake Town, Kolkata"),
    "700041":(22.5900,88.4200,"Dum Dum, Kolkata"),
    "700053":(22.6500,88.3300,"Barasat, Kolkata"),
    "700054":(22.5400,88.3100,"Kasba, Kolkata"),
    "700059":(22.5200,88.4000,"Garia, Kolkata"),
    "700060":(22.5800,88.4300,"Salt Lake, Kolkata"),
    "700064":(22.6000,88.3800,"Birati, Kolkata"),
    "700075":(22.5726,88.4100,"VIP Road, Kolkata"),
    "700091":(22.5000,88.2800,"Joka Phase 2, Kolkata"),
    "700094":(22.5726,88.3200,"Majerhat, Kolkata"),
    "700106":(22.6000,88.4200,"New Town, Kolkata"),
    # Pune
    "411001":(18.5204,73.8567,"Pune Camp"),
    "411002":(18.5300,73.8700,"Dhole Patil, Pune"),
    "411004":(18.5523,73.9143,"Kalyani Nagar, Pune"),
    "411005":(18.5200,73.8300,"Shivajinagar, Pune"),
    "411006":(18.5314,73.8480,"Kothrud, Pune"),
    "411007":(18.5000,73.8200,"Narayan Peth, Pune"),
    "411008":(18.5679,73.7143,"Pimpri, Pune"),
    "411009":(18.4638,73.8687,"Wanowrie, Pune"),
    "411011":(18.4529,73.8496,"Bibvewadi, Pune"),
    "411013":(18.4800,73.9300,"Kondhwa, Pune"),
    "411014":(18.5900,73.9200,"Viman Nagar, Pune"),
    "411015":(18.5100,73.8600,"Erandwane, Pune"),
    "411016":(18.5400,73.8900,"Hadapsar, Pune"),
    "411017":(18.4300,73.9000,"Undri, Pune"),
    "411018":(18.5600,73.7900,"Chinchwad, Pune"),
    "411021":(18.6000,73.8000,"Bhosari, Pune"),
    "411028":(18.5200,73.7600,"Wakad, Pune"),
    "411033":(18.4900,73.8700,"NIBM, Pune"),
    "411041":(18.6100,73.9100,"Khardi, Pune"),
    "411045":(18.5600,73.8700,"Nagar Road, Pune"),
    "411057":(18.5800,73.8200,"Aundh, Pune"),
    # Ahmedabad
    "380001":(23.0225,72.5714,"Ahmedabad"),
    "380004":(23.0469,72.5340,"Navrangpura, Ahmedabad"),
    "380005":(22.9977,72.5969,"Maninagar, Ahmedabad"),
    "380006":(23.0733,72.5114,"Chandkheda, Ahmedabad"),
    "380007":(23.0300,72.5500,"Shahibaug, Ahmedabad"),
    "380008":(23.0400,72.5800,"Naranpura, Ahmedabad"),
    "380009":(23.0600,72.5200,"Ellis Bridge, Ahmedabad"),
    "380013":(22.9900,72.6000,"Vatva, Ahmedabad"),
    "380014":(23.0200,72.6400,"Bapunagar, Ahmedabad"),
    "380015":(23.0100,72.5700,"Gomtipur, Ahmedabad"),
    "380018":(22.9600,72.5300,"Isanpur, Ahmedabad"),
    "380019":(23.0600,72.5700,"Ranip, Ahmedabad"),
    "380021":(23.0000,72.5600,"Ghodasar, Ahmedabad"),
    "380022":(22.9900,72.5100,"Odhav, Ahmedabad"),
    "380024":(23.0500,72.5000,"New Ranip, Ahmedabad"),
    "380026":(23.0700,72.6200,"Kubernagar, Ahmedabad"),
    "380028":(23.1100,72.5200,"Hansol, Ahmedabad"),
    "380050":(23.1000,72.5700,"Nava Vadaj, Ahmedabad"),
    "380051":(23.0000,72.6100,"Gota, Ahmedabad"),
    "380054":(23.0100,72.6200,"Nikol, Ahmedabad"),
    "380055":(23.0400,72.6600,"Naroda, Ahmedabad"),
    "380058":(23.0800,72.5800,"Motera, Ahmedabad"),
    "380059":(23.0900,72.5400,"Sabarmati, Ahmedabad"),
    "380060":(23.0500,72.5100,"Bodakdev, Ahmedabad"),
    "380061":(23.0300,72.5000,"Satellite, Ahmedabad"),
    "380063":(23.0100,72.5300,"Prahlad Nagar, Ahmedabad"),
    # Jaipur
    "302001":(26.9124,75.7873,"Jaipur"),
    "302002":(26.9200,75.8200,"Malviya Nagar, Jaipur"),
    "302003":(26.8800,75.8100,"Sanganer, Jaipur"),
    "302004":(26.9000,75.7500,"Adarsh Nagar, Jaipur"),
    "302006":(26.9400,75.7500,"Bani Park, Jaipur"),
    "302011":(26.8500,75.7800,"Jagatpura, Jaipur"),
    "302012":(26.9700,75.7700,"Jhotwara, Jaipur"),
    "302015":(26.9300,75.8000,"Mansarovar, Jaipur"),
    "302017":(26.8300,75.8100,"Sitapura, Jaipur"),
    "302019":(26.9100,75.7200,"Vaishali Nagar, Jaipur"),
    "302020":(26.8800,75.7500,"Pratap Nagar, Jaipur"),
    "302021":(26.9100,75.8400,"Tonk Road, Jaipur"),
    "302022":(26.9400,75.8100,"Raja Park, Jaipur"),
    "302033":(26.9700,75.8100,"Triveni Nagar, Jaipur"),
    # Surat
    "395001":(21.1702,72.8311,"Surat"),
    "395002":(21.2000,72.8500,"Katargam, Surat"),
    "395003":(21.2100,72.8200,"Rander, Surat"),
    "395004":(21.1400,72.8100,"Udhna, Surat"),
    "395005":(21.1700,72.8700,"Varachha, Surat"),
    "395006":(21.1900,72.7900,"Piplod, Surat"),
    "395007":(21.2200,72.8700,"Kamrej, Surat"),
    "395009":(21.1900,72.8300,"Adajan, Surat"),
    "395010":(21.1600,72.8600,"Athwa Lines, Surat"),
    # Lucknow
    "226001":(26.8467,80.9462,"Lucknow"),
    "226002":(26.8700,80.9700,"Hazratganj, Lucknow"),
    "226003":(26.8200,80.9200,"Aliganj, Lucknow"),
    "226004":(26.8900,80.9100,"Indira Nagar, Lucknow"),
    "226005":(26.8100,80.9800,"Gomti Nagar, Lucknow"),
    "226006":(26.8600,80.9500,"Chowk, Lucknow"),
    "226007":(26.8400,80.9300,"Mahanagar, Lucknow"),
    "226008":(26.8800,80.9600,"Rajajipuram, Lucknow"),
    "226010":(26.8500,80.9800,"Vikas Nagar, Lucknow"),
    "226012":(26.8300,81.0100,"Eldeco Colony, Lucknow"),
    "226016":(26.8600,81.0000,"Kursi Road, Lucknow"),
    "226020":(26.8200,80.9600,"Ashiana, Lucknow"),
    "226021":(26.8300,80.9500,"Faizabad Road, Lucknow"),
    "226022":(26.8200,81.0200,"Sultanpur Road, Lucknow"),
    # Chandigarh
    "160001":(30.7333,76.7794,"Sector 1, Chandigarh"),
    "160002":(30.7446,76.7891,"Sector 2, Chandigarh"),
    "160003":(30.7553,76.8013,"Sector 3, Chandigarh"),
    "160008":(30.7550,76.8100,"Sector 8, Chandigarh"),
    "160009":(30.7200,76.8200,"Sector 9, Chandigarh"),
    "160010":(30.7050,76.7500,"Sector 10, Chandigarh"),
    "160011":(30.7700,76.7600,"Sector 11, Chandigarh"),
    "160014":(30.7550,76.7700,"Sector 14, Chandigarh"),
    "160015":(30.7400,76.7600,"Sector 15, Chandigarh"),
    "160017":(30.7300,76.7800,"Sector 17, Chandigarh"),
    "160018":(30.7200,76.7700,"Sector 18, Chandigarh"),
    "160019":(30.7100,76.7600,"Sector 19, Chandigarh"),
    "160022":(30.7046,76.7179,"Sector 22, Chandigarh"),
    "160047":(30.7046,76.7179,"Mohali, Punjab"),
    # Noida
    "201301":(28.5355,77.3910,"Noida Sector 1"),
    "201302":(28.5700,77.3210,"Noida Sector 18"),
    "201303":(28.5200,77.4000,"Noida Sector 62"),
    "201304":(28.4744,77.5040,"Greater Noida"),
    "201305":(28.5900,77.3600,"Noida Sector 37"),
    "201306":(28.5500,77.3400,"Noida Sector 50"),
    "201307":(28.5100,77.4300,"Noida Sector 100"),
    "201308":(28.6000,77.4000,"Noida Sector 45"),
    "201309":(28.5800,77.3200,"Noida Sector 63"),
    # Gurgaon
    "122001":(28.4595,77.0266,"Gurgaon"),
    "122002":(28.4830,77.0890,"DLF Phase 1, Gurgaon"),
    "122003":(28.4207,77.0213,"Sushant Lok, Gurgaon"),
    "122006":(28.5020,77.0500,"Palam Vihar, Gurgaon"),
    "122007":(28.4300,77.0700,"South City, Gurgaon"),
    "122008":(28.5200,77.1000,"Sector 14, Gurgaon"),
    "122009":(28.4700,77.0100,"Badshahpur, Gurgaon"),
    "122010":(28.4100,77.0500,"Sector 70, Gurgaon"),
    "122015":(28.4500,76.9800,"Manesar, Gurgaon"),
    "122016":(28.4600,77.0600,"Sector 15, Gurgaon"),
    "122017":(28.5000,77.0800,"Sector 57, Gurgaon"),
    "122018":(28.4800,77.0900,"Golf Course Road, Gurgaon"),
    "122022":(28.4700,77.0700,"MG Road, Gurgaon"),
    # Patna
    "800001":(25.5941,85.1376,"Patna"),
    "800002":(25.6200,85.1600,"Rajendra Nagar, Patna"),
    "800003":(25.5700,85.1100,"Kankarbagh, Patna"),
    "800004":(25.6000,85.0900,"Gandhi Maidan, Patna"),
    "800007":(25.6300,85.0800,"Boring Road, Patna"),
    "800009":(25.6100,85.1300,"Patna Sahib, Patna"),
    "800014":(25.5900,85.1500,"Danapur, Patna"),
    "800020":(25.6000,85.1700,"Anisabad, Patna"),
    # Bhopal
    "462001":(23.2599,77.4126,"Bhopal"),
    "462002":(23.2900,77.4300,"Arera Colony, Bhopal"),
    "462003":(23.2300,77.3900,"Habibganj, Bhopal"),
    "462007":(23.2700,77.3700,"Kolar Road, Bhopal"),
    "462008":(23.2100,77.4500,"Bairagarh, Bhopal"),
    "462011":(23.2700,77.4600,"Ayodhya Nagar, Bhopal"),
    "462016":(23.2800,77.4800,"Bawadia Kalan, Bhopal"),
    "462020":(23.2400,77.4200,"Shahpura, Bhopal"),
    "462026":(23.2600,77.4900,"Hoshangabad Road, Bhopal"),
    "462031":(23.2800,77.4000,"Karond, Bhopal"),
    "462039":(23.2600,77.4200,"Bhopal Sadar, Bhopal"),
    # Indore
    "452001":(22.7196,75.8577,"Indore"),
    "452002":(22.7500,75.8800,"Vijay Nagar, Indore"),
    "452003":(22.6900,75.8300,"Lasudia, Indore"),
    "452006":(22.7400,75.8200,"Rajendra Nagar, Indore"),
    "452007":(22.6800,75.8800,"Bicholi Mardana, Indore"),
    "452008":(22.7200,75.9200,"Rau, Indore"),
    "452009":(22.7300,75.8700,"Palasia, Indore"),
    "452010":(22.6800,75.8200,"Kanadiya, Indore"),
    "452011":(22.7100,75.8900,"Bhawarkuan, Indore"),
    "452015":(22.7600,75.9000,"Aerodrome Area, Indore"),
    "452018":(22.7500,75.8100,"Scheme No 54, Indore"),
    "452020":(22.7400,75.8500,"Annapurna, Indore"),
    # Nagpur
    "440001":(21.1458,79.0882,"Nagpur"),
    "440002":(21.1700,79.1100,"Gandhibagh, Nagpur"),
    "440003":(21.1200,79.0600,"Sitabuldi, Nagpur"),
    "440009":(21.1800,79.0500,"Dharampeth, Nagpur"),
    "440010":(21.1000,79.1200,"Manewada, Nagpur"),
    "440012":(21.1500,79.0800,"Wardhaman Nagar, Nagpur"),
    "440013":(21.1600,79.0900,"Ramdaspeth, Nagpur"),
    "440014":(21.1900,79.0700,"Pratap Nagar, Nagpur"),
    "440015":(21.1400,79.0400,"Laxmi Nagar, Nagpur"),
    "440017":(21.1300,79.0500,"Jaripatka, Nagpur"),
    "440019":(21.1700,79.0300,"Sakkardara, Nagpur"),
    "440022":(21.1100,79.0800,"Nandanvan, Nagpur"),
    "440025":(21.1000,79.0600,"Besa, Nagpur"),
    "440027":(21.1300,79.1100,"Hudkeshwar, Nagpur"),
    # Kochi
    "682001":(9.9312,76.2673,"Ernakulam, Kochi"),
    "682002":(9.9600,76.2900,"Fort Kochi"),
    "682004":(9.9800,76.2200,"Mattancherry, Kochi"),
    "682005":(9.9000,76.2400,"Palarivattom, Kochi"),
    "682006":(9.9600,76.3200,"Kakkanad, Kochi"),
    "682011":(9.9200,76.2700,"Vyttila, Kochi"),
    "682013":(9.9400,76.2600,"Thoppumpady, Kochi"),
    "682016":(9.9600,76.3500,"Edapally, Kochi"),
    "682018":(9.9800,76.2900,"Panampilly Nagar, Kochi"),
    "682019":(9.9700,76.3100,"Aluva, Kochi"),
    "682020":(9.9100,76.2800,"Thrikkakara, Kochi"),
    "682021":(9.9300,76.2500,"Kadavanthra, Kochi"),
    "682022":(9.9500,76.3100,"Chottanikkara, Kochi"),
    "682025":(10.0200,76.2800,"Perumbavoor, Kochi"),
    "682028":(9.9700,76.2600,"Marine Drive, Kochi"),
    "682030":(9.9100,76.3300,"Ponekkara, Kochi"),
    "682031":(9.8900,76.2800,"Maradu, Kochi"),
    "682032":(9.9600,76.3800,"Angamaly, Kochi"),
    # Coimbatore
    "641001":(11.0168,76.9558,"Coimbatore"),
    "641002":(11.0400,76.9800,"R.S.Puram, Coimbatore"),
    "641003":(10.9900,76.9300,"Gandhipuram, Coimbatore"),
    "641004":(11.0600,76.9200,"Saibaba Colony, Coimbatore"),
    "641005":(10.9700,76.9700,"Singanallur, Coimbatore"),
    "641006":(11.0200,76.9400,"Town Hall, Coimbatore"),
    "641007":(11.0700,76.9100,"Peelamedu, Coimbatore"),
    "641008":(11.0500,76.9600,"Race Course, Coimbatore"),
    "641009":(11.0100,76.9500,"Kuniyamuthur, Coimbatore"),
    "641010":(11.0300,76.9700,"Selvapuram, Coimbatore"),
    "641011":(11.0600,77.0000,"Kovaipudur, Coimbatore"),
    "641012":(11.0900,76.9800,"Podanur, Coimbatore"),
    "641014":(11.0700,76.9400,"Saravanampatti, Coimbatore"),
    "641018":(11.0500,76.9900,"Kovaipudur 2, Coimbatore"),
    "641021":(11.0000,76.9400,"Vadavalli, Coimbatore"),
    "641025":(11.0300,76.9000,"Thudiyalur, Coimbatore"),
    "641028":(11.0800,76.9000,"Vilankurichi, Coimbatore"),
    "641035":(11.0600,76.9700,"Kurichi, Coimbatore"),
    "641041":(11.0500,76.9100,"Kovaipudur 3, Coimbatore"),
    "641046":(11.0400,76.9200,"Ganapathy, Coimbatore"),
    # Visakhapatnam
    "530001":(17.6868,83.2185,"Visakhapatnam"),
    "530002":(17.7100,83.2400,"Waltair, Visakhapatnam"),
    "530003":(17.6600,83.2000,"Gajuwaka, Visakhapatnam"),
    "530004":(17.7300,83.1900,"Bheemunipatnam"),
    "530007":(17.6400,83.2600,"Pendurthi, Visakhapatnam"),
    "530008":(17.7200,83.3200,"MVP Colony, Visakhapatnam"),
    "530009":(17.7000,83.2600,"BHPV Colony, Visakhapatnam"),
    "530012":(17.7500,83.2800,"Kommadi, Visakhapatnam"),
    "530013":(17.7100,83.3100,"Aganampudi, Visakhapatnam"),
    "530017":(17.6800,83.2100,"Old Town, Visakhapatnam"),
    "530022":(17.6900,83.2700,"Akkayyapalem, Visakhapatnam"),
    "530024":(17.7200,83.2600,"Madhurawada, Visakhapatnam"),
    "530026":(17.7400,83.2400,"Rushikonda, Visakhapatnam"),
    "530029":(17.6700,83.2500,"Dwaraka Nagar, Visakhapatnam"),
    "530041":(17.7300,83.3000,"Anandapuram, Visakhapatnam"),
    # Guwahati
    "781001":(26.1445,91.7362,"Guwahati"),
    "781003":(26.1700,91.7600,"Silpukhuri, Guwahati"),
    "781005":(26.1200,91.7100,"Jalukbari, Guwahati"),
    "781006":(26.1800,91.7000,"Sixmile, Guwahati"),
    "781007":(26.1100,91.7500,"Ganeshguri, Guwahati"),
    "781009":(26.1600,91.7800,"Ulubari, Guwahati"),
    "781010":(26.1400,91.8200,"Narengi, Guwahati"),
    "781011":(26.1500,91.8000,"GMCH Area, Guwahati"),
    "781014":(26.1200,91.7800,"Lachit Nagar, Guwahati"),
    "781021":(26.1800,91.7500,"VIP Road, Guwahati"),
    # Ranchi
    "834001":(23.3441,85.3096,"Ranchi"),
    "834002":(23.3700,85.3300,"Doranda, Ranchi"),
    "834003":(23.3200,85.2900,"Hindpiri, Ranchi"),
    "834004":(23.3600,85.2700,"Morabadi, Ranchi"),
    "834005":(23.3500,85.3100,"Bariatu, Ranchi"),
    "834006":(23.3700,85.3500,"Harmu, Ranchi"),
    "834008":(23.3000,85.3100,"Tupudana, Ranchi"),
    "834009":(23.3800,85.3200,"Lalpur, Ranchi"),
    "834010":(23.3900,85.3000,"Ratu Road, Ranchi"),
    # Jamshedpur
    "831001":(22.8046,86.2029,"Jamshedpur"),
    "831002":(22.8300,86.2200,"Bistupur, Jamshedpur"),
    "831003":(22.7800,86.1800,"Adityapur, Jamshedpur"),
    "831004":(22.8100,86.2400,"Jugsalai, Jamshedpur"),
    "831005":(22.7900,86.2100,"Telco, Jamshedpur"),
    "831006":(22.8400,86.1600,"Mango, Jamshedpur"),
    # Bhubaneswar
    "751001":(20.2961,85.8245,"Bhubaneswar"),
    "751002":(20.3200,85.8400,"Unit IV, Bhubaneswar"),
    "751003":(20.2700,85.8000,"Saheed Nagar, Bhubaneswar"),
    "751004":(20.3000,85.7800,"Vani Vihar, Bhubaneswar"),
    "751005":(20.2800,85.8200,"Jaydev Vihar, Bhubaneswar"),
    "751006":(20.3100,85.8100,"Nayapalli, Bhubaneswar"),
    "751007":(20.2600,85.8500,"AIIMS, Bhubaneswar"),
    "751009":(20.3400,85.8300,"Damana, Bhubaneswar"),
    "751010":(20.2800,85.8700,"Infocity, Bhubaneswar"),
    "751013":(20.3500,85.8200,"Pokhariput, Bhubaneswar"),
    "751015":(20.2900,85.8600,"KIIT, Bhubaneswar"),
    "751016":(20.2500,85.8400,"Patia, Bhubaneswar"),
    "751024":(20.3000,85.8100,"Khandagiri, Bhubaneswar"),
    "751025":(20.2700,85.7900,"Mancheswar, Bhubaneswar"),
    "751030":(20.3600,85.8100,"Rasulgarh, Bhubaneswar"),
    # Dehradun
    "248001":(30.3165,78.0322,"Dehradun"),
    "248002":(30.3400,78.0500,"Dalanwala, Dehradun"),
    "248003":(30.2900,78.0100,"Clement Town, Dehradun"),
    "248007":(30.3600,78.0700,"Rajpur Road, Dehradun"),
    "248008":(30.3200,78.0400,"Prem Nagar, Dehradun"),
    "248009":(30.3300,78.0200,"Ballupur, Dehradun"),
    # Varanasi
    "221001":(25.3176,82.9739,"Varanasi"),
    "221002":(25.3400,83.0000,"Sigra, Varanasi"),
    "221003":(25.2900,82.9500,"Lanka, Varanasi"),
    "221004":(25.3300,82.9300,"Orderly Bazar, Varanasi"),
    "221005":(25.3000,82.9800,"Assi, Varanasi"),
    "221006":(25.3200,82.9600,"Maldahia, Varanasi"),
    "221007":(25.3500,83.0100,"Sarnath, Varanasi"),
    "221010":(25.3700,82.9500,"Shivpur, Varanasi"),
    # Amritsar
    "143001":(31.6340,74.8723,"Amritsar"),
    "143002":(31.6500,74.9000,"Ranjit Avenue, Amritsar"),
    "143006":(31.6200,74.8500,"Cantonment, Amritsar"),
    "143007":(31.6700,74.8400,"Sultanwind, Amritsar"),
    # Gaya (Bihar)
    "823001":(24.7914,85.0002,"Gaya"),
    "823002":(24.8100,85.0200,"Bodh Gaya Area"),
    "823003":(24.7700,84.9800,"Manpur, Gaya"),
    "824101":(24.9700,85.0300,"Aurangabad, Bihar"),
    "824143":(24.7500,84.9700,"Gaya Rural"),
    # Agra
    "282001":(27.1767,78.0081,"Agra"),
    "282002":(27.2000,78.0300,"Tajganj, Agra"),
    "282003":(27.1500,77.9800,"Sikandra, Agra"),
    "282004":(27.1900,78.0500,"Sadar Agra"),
    "282005":(27.2200,78.0700,"Kamla Nagar, Agra"),
    "282007":(27.1700,78.0200,"Balkeshwar, Agra"),
    "282010":(27.2000,78.0100,"Raja Mandi, Agra"),
    # Mysuru
    "570001":(12.2958,76.6394,"Mysuru"),
    "570002":(12.3200,76.6600,"Hebbal, Mysuru"),
    "570008":(12.2700,76.6100,"Vijayanagar, Mysuru"),
    "570009":(12.3100,76.6900,"Kuvempunagar, Mysuru"),
    "570010":(12.3000,76.6500,"Saraswathipuram, Mysuru"),
    "570015":(12.2800,76.6800,"Lakshmipuram, Mysuru"),
    "570016":(12.3100,76.6200,"Yadavagiri, Mysuru"),
    "570017":(12.2600,76.6400,"JLB Road, Mysuru"),
    "570019":(12.3200,76.6100,"Jayalakshmipuram, Mysuru"),
    "570020":(12.3000,76.6700,"Gokulam, Mysuru"),
    # Prayagraj (Allahabad)
    "211001":(25.4358,81.8463,"Prayagraj"),
    "211002":(25.4600,81.8700,"Civil Lines, Prayagraj"),
    "211003":(25.4100,81.8200,"Naini, Prayagraj"),
    "211004":(25.4500,81.8100,"George Town, Prayagraj"),
    "211006":(25.4700,81.8900,"Bamrauli, Prayagraj"),
    "211011":(25.4200,81.8600,"Ashok Nagar, Prayagraj"),
    # Meerut
    "250001":(28.9845,77.7064,"Meerut"),
    "250002":(29.0100,77.7300,"Shastri Nagar, Meerut"),
    "250003":(28.9600,77.6800,"Pallavpuram, Meerut"),
    "250004":(29.0000,77.6600,"Kanker Khera, Meerut"),
    "250005":(28.9900,77.7400,"Ganga Nagar, Meerut"),
    # Ludhiana
    "141001":(30.9010,75.8573,"Ludhiana"),
    "141002":(30.9300,75.8800,"Model Town, Ludhiana"),
    "141003":(30.8700,75.8300,"Haibowal, Ludhiana"),
    "141004":(30.9500,75.8200,"Bhai Randhir Singh Nagar, Ludhiana"),
    "141005":(30.8500,75.8700,"Dugri, Ludhiana"),
    "141006":(30.9200,75.8600,"BRS Nagar, Ludhiana"),
    "141007":(30.8900,75.8100,"Focal Point, Ludhiana"),
    "141008":(30.9100,75.9000,"Dhandari Kalan, Ludhiana"),
    "141010":(30.9400,75.8700,"Chandigarh Road, Ludhiana"),
    "141012":(30.8600,75.8500,"Pakhowal, Ludhiana"),
    "141013":(30.9600,75.8100,"Sherpur, Ludhiana"),
}



def _pincode_to_coords(pin: str):
    """Pin code → (lat, lng, label).  6-tier fallback for all India coverage:
    -1) Static local database (instant, no API)
    0) India Post API → reliable district/state info
    1) Nominatim postalcode direct lookup
    2) OpenDataSoft India PIN database
    3) India Post area name → Nominatim search
    4) District → Nominatim search (last resort)
    """
    import urllib.parse
    nom_headers = {"User-Agent": "SwiggyOfferBot/2.0 (contact@example.com)"}

    # ── Tier -1: Static local database — INSTANT, zero latency ───────────────
    if pin in _PIN_STATIC:
        lat, lng, label = _PIN_STATIC[pin]
        logger.info(f"Static DB hit for PIN {pin}: {label}")
        return lat, lng, label

    area_name = district = state = taluk = ""

    # ── Tier 0: India Post API — most reliable for all India PINs ────────────
    try:
        r2 = requests.get(
            f"https://api.postalpincode.in/pincode/{pin}",
            timeout=12, headers=nom_headers
        )
        if r2.status_code == 200:
            j = r2.json()
            if j and j[0].get("Status") == "Success" and j[0].get("PostOffice"):
                po        = j[0]["PostOffice"][0]
                area_name = po.get("Name", "")
                district  = po.get("District", "")
                taluk     = po.get("Taluk", "")
                state     = po.get("State", "")
                logger.info(f"India Post: {area_name}, {district}, {state} for PIN {pin}")
    except Exception as e:
        logger.debug(f"indiapost api error {pin}: {e}")

    def _nom_search(q: str):
        """Nominatim geocoding with retry."""
        for attempt in range(2):
            try:
                time.sleep(0.5 * attempt)  # be polite on retry
                r = requests.get(
                    f"https://nominatim.openstreetmap.org/search"
                    f"?q={urllib.parse.quote(q)}&format=json&limit=3&countrycodes=in",
                    headers=nom_headers, timeout=12
                )
                if r.status_code == 200:
                    d = r.json()
                    if d:
                        # Prefer results with higher importance (better match)
                        best = sorted(d, key=lambda x: -float(x.get("importance", 0)))[0]
                        return float(best["lat"]), float(best["lon"])
            except Exception as e:
                logger.debug(f"nominatim search error '{q}' attempt {attempt}: {e}")
        return None, None

    # ── Tier 1: Nominatim direct postal lookup ────────────────────────────────
    try:
        r = requests.get(
            f"https://nominatim.openstreetmap.org/search"
            f"?postalcode={pin}&country=India&format=json&limit=1",
            headers=nom_headers, timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                label = area_name or data[0].get("display_name", pin)
                label = label.split(",")[0].strip()
                logger.info(f"Nominatim postal hit for PIN {pin}: {label}")
                return float(data[0]["lat"]), float(data[0]["lon"]), label
    except Exception as e:
        logger.debug(f"nominatim postalcode error {pin}: {e}")

    # Common label for remaining tiers
    label = f"{area_name}, {district}" if area_name and district else (area_name or district or pin)

    # ── Tier 2: OpenDataSoft India Pincode API ────────────────────────────────
    try:
        url = (f"https://public.opendatasoft.com/api/records/1.0/search/"
               f"?dataset=georef-india-city&q={pin}&rows=1")
        rd = requests.get(url, timeout=10)
        if rd.status_code == 200:
            recs = rd.json().get("records", [])
            if recs:
                geo = recs[0].get("fields", {}).get("geo_point_2d")
                if geo and len(geo) == 2:
                    logger.info(f"OpenDataSoft hit for PIN {pin}")
                    return float(geo[0]), float(geo[1]), label
    except Exception as e:
        logger.debug(f"opendatasoft error {pin}: {e}")

    # ── Tier 3: area name + district search ──────────────────────────────────
    if area_name and district:
        for q in [
            f"{area_name}, {district}, {state}, India",
            f"{area_name}, {state}, India",
            f"{district}, {state}, India",
        ]:
            lat, lng = _nom_search(q)
            if lat:
                logger.info(f"Nominatim name search hit: {q}")
                return lat, lng, label

    # ── Tier 4: taluk / district fallback ────────────────────────────────────
    for q in [
        f"{taluk}, {district}, {state}, India" if taluk else None,
        f"{district}, {state}, India" if district and state else None,
        f"{state}, India" if state else None,
    ]:
        if not q:
            continue
        lat, lng = _nom_search(q)
        if lat:
            logger.info(f"Nominatim district fallback hit: {q}")
            return lat, lng, label

    logger.warning(f"All geocoding tiers failed for PIN {pin}")
    return None, None, None


def _nearby_points(lat: float, lng: float):
    """Central point ke aas-paas scan points generate karo (~25km radius).
    5 rings: 2km, 5km, 8km, 13km, 20km — small town + district full coverage."""
    pts = [(lat, lng)]  # center
    # Wider rings for better coverage across all India towns
    for delta in [0.018, 0.045, 0.075, 0.12, 0.18]:
        pts += [
            (lat + delta, lng        ),
            (lat - delta, lng        ),
            (lat,         lng + delta),
            (lat,         lng - delta),
            (lat + delta, lng + delta),
            (lat + delta, lng - delta),
            (lat - delta, lng + delta),
            (lat - delta, lng - delta),
        ]
    # Extra diagonal ring for sparse/semi-urban areas
    for delta in [0.09, 0.15]:
        pts += [
            (lat + delta*0.7, lng + delta),
            (lat - delta*0.7, lng + delta),
            (lat + delta*0.7, lng - delta),
            (lat - delta*0.7, lng - delta),
        ]
    # remove duplicates while preserving order
    seen = set()
    unique = []
    for p in pts:
        k = (round(p[0], 3), round(p[1], 3))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def _fetch_raw_coords(lat: float, lng: float, label: str) -> list:
    """Specific lat/lng se offers fetch karo — pin code based search ke liye."""
    cache_key = f"pin:{lat:.4f}:{lng:.4f}"
    now = time.time()
    if cache_key in _CACHE and (now - _CACHE[cache_key]["ts"]) < CACHE_TTL:
        return _CACHE[cache_key]["data"]

    points = _nearby_points(lat, lng)
    all_restaurants: dict = {}

    def _merge(r_list):
        for r in r_list:
            name = r["name"]
            if name not in all_restaurants or r["score"] > all_restaurants[name]["score"]:
                all_restaurants[name] = r

    # Global pool use karo — nested pool nahi
    futs = {}
    for pt in points:
        futs[_FETCH_POOL.submit(_fetch_one, *pt)]        = "regular"
        futs[_FETCH_POOL.submit(_fetch_discounted, *pt)] = "discount"
    for fut in as_completed(futs):
        try:
            _merge(fut.result())
        except Exception as e:
            logger.debug(f"pincode worker error: {e}")

    result = list(all_restaurants.values())
    logger.info(f"Pin code {label}: {len(result)} restaurants")
    _CACHE[cache_key] = {"ts": now, "data": result}
    return result


async def _async_fetch_coords(lat: float, lng: float, label: str) -> list:
    """Non-blocking version of _fetch_raw_coords."""
    cache_key = f"pin:{lat:.4f}:{lng:.4f}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]
    # asyncio single-threaded event loop mein dict check safe hai
    if cache_key not in _CITY_LOCKS:
        _CITY_LOCKS[cache_key] = asyncio.Lock()
    async with _CITY_LOCKS[cache_key]:
        now = time.time()
        cached = _CACHE.get(cache_key)
        if cached and (now - cached["ts"]) < CACHE_TTL:
            return cached["data"]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_FETCH_POOL, _fetch_raw_coords, lat, lng, label)


def _build_pincode_page(pin: str, area: str, offers: list, total: int, page: int = 0) -> str:
    """Pin code / location results ke liye formatted page."""
    PER   = 8
    ts    = _ts()

    # Re-sort by real deal value
    offers_sorted = sorted(offers, key=lambda r: -_deal_value(r))

    start = page * PER
    chunk = offers_sorted[start: start + PER]
    total_pages = max(1, (total + PER - 1) // PER)

    pin_label = f"📡 *Live Location*" if pin == "📡" else f"📮 *Pin Code {pin}*"
    lines = [
        f"{pin_label} — *{area}*",
        f"✅ *{total} restaurants* mein offers mile!  ·  🕐 {ts}",
        f"📄 Page {page+1}/{total_pages}",
    ]

    if page == 0:
        banner = _hot_deals_banner(offers_sorted)
        if banner:
            lines.append("")
            lines.append(banner)
        lines.append("")

    for i, r in enumerate(chunk, start + 1):
        lines.append(_card(i, r, show_demo=True))
        lines.append("")

    if not chunk:
        lines.append("_Koi deal nahi mila is area mein._")
    return "\n".join(lines)


def _budget_filter(all_r: list, budget: int) -> list:
    """Pure filter — koi network call nahi, already-fetched list pe kaam karta hai."""
    out = []
    for r in all_r:
        c = r["cost_num"]
        if not c or c > budget * 2: continue   # cost_num = cost for TWO people
        saved = 0
        if r["pct"]:
            raw = int(c * r["pct"] / 100)
            saved = min(raw, r["upto"]) if r["upto"] else raw
        elif r["flat"] and (not r["min_order"] or c >= r["min_order"]):
            saved = r["flat"]
        out.append({**r, "saved": saved, "final": max(0, c - saved)})
    return sorted(out, key=lambda x: -x["saved"])


def get_budget(city_key, budget):
    return _budget_filter(get_all(city_key), budget)


async def async_search_rest(city_key, q):
    """Non-blocking search — event loop free rehta hai."""
    all_r = await _async_get_all(city_key)
    matched = _srt([r for r in all_r if q.lower() in r["name"].lower()])
    return matched, len(all_r)


def search_rest(city_key, q):
    all_r = get_all(city_key)
    matched = _srt([r for r in all_r if q.lower() in r["name"].lower()])
    return matched, len(all_r)


# ══════════════════════════════════════════════════════════════════════════════
#  CARD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _rest_url(r: dict, city_slug: str) -> str:
    """Swiggy restaurant ka direct URL banao."""
    if r.get("rest_id") and r.get("slug") and city_slug:
        return f"https://www.swiggy.com/city/{city_slug}/{r['slug']}-rest{r['rest_id']}"
    # Fallback — slug ya city_slug missing ho to bhi direct restaurant URL
    if r.get("rest_id") and r.get("slug"):
        return f"https://www.swiggy.com/restaurants/{r['slug']}-rest{r['rest_id']}"
    if r.get("rest_id"):
        return f"https://www.swiggy.com/restaurants/{r['rest_id']}"
    return ""


_DEMO_AMOUNTS = [99, 149, 199, 249, 299, 399, 499]


def _deal_value(r: dict) -> int:
    """Realistic max savings at common order amounts — used for sorting."""
    best = 0
    for amt in _DEMO_AMOUNTS:
        if r["min_order"] and amt < r["min_order"]:
            continue
        if r["flat"]:
            sv = r["flat"]
        elif r["pct"]:
            sv = int(amt * r["pct"] / 100)
            if r["upto"]: sv = min(sv, r["upto"])
        else:
            sv = 0
        best = max(best, sv)
    return best


def _deal_demo(r: dict) -> str:
    """'Order ₹X → Save ₹Y → Pay ₹Z' — only shown when min_order is
    explicitly known from the API (never guessed). Uses min_order as
    the demo amount so the calculation is always realistic."""
    mo = r["min_order"]
    if not mo:
        # min_order unknown → don't fabricate a savings calculation
        return ""
    # Pick the lowest DEMO_AMOUNT that meets min_order
    for amt in _DEMO_AMOUNTS:
        if amt < mo:
            continue
        sv = 0
        if r["flat"]:
            sv = r["flat"]
        elif r["pct"]:
            sv = int(amt * r["pct"] / 100)
            if r["upto"]: sv = min(sv, r["upto"])
        # Only show if savings are meaningful AND price stays positive
        if sv >= 30 and sv < amt:
            return f"🤑 ₹{amt} order → Save ₹{sv} → Pay ₹{amt - sv}"
    return ""


def _badge(r):
    """Show raw Swiggy offer text as-is — no reconstruction, no guessing.
    Just pick the right emoji prefix based on offer type."""
    text   = (r.get("offer") or "").strip()
    coupon = (r.get("coupon") or "").strip()

    if not text:
        return ""

    # Emoji prefix: percentage vs flat vs generic
    if r.get("pct"):
        prefix = "🔥"
    elif r.get("flat"):
        prefix = "💸"
    else:
        prefix = "🏷️"

    badge = f"{prefix} {text[:80]}"
    if coupon:
        badge += f"  🎟️ `{coupon}`"
    return badge


def _card(n, r, city_slug="", show_demo=False):
    """Clean grouped card — matches reference layout, real data only."""
    url  = _rest_url(r, city_slug)
    name = r["name"]
    name_part = f"[{name}]({url})" if url else f"*{name}*"
    lines = [f"*{n}.* {name_part}"]

    cuisine = (r.get("cuisine") or "").strip()
    if cuisine:
        lines.append(f"🍴 _{cuisine[:70]}_")

    offer_line = ""
    if r.get("pct"):
        offer_line = f"🔥 *{r['pct']}% OFF*"
        if r.get("upto"):
            offer_line += f" up to ₹{r['upto']}"
    elif r.get("flat"):
        offer_line = f"💸 *₹{r['flat']} OFF*"
        if r.get("min_order"):
            offer_line += f" above ₹{r['min_order']}"
    elif r.get("offer"):
        offer_line = f"🏷️ {r['offer'][:70]}"
    if offer_line:
        lines.append(offer_line)

    if r.get("coupon"):
        lines.append(f"🎟️ `{r['coupon']}`")

    meta = []
    if r.get("rating"): meta.append(f"⭐{r['rating']}")
    if r.get("votes"):  meta.append(r["votes"])
    if r.get("time"):   meta.append(f"⏱{r['time']}m")
    if r.get("cost"):   meta.append(f"₹{r['cost']} for two")
    if meta:
        lines.append(" · ".join(meta))

    area = r.get("area", "")
    if area:
        lines.append(f"📍 _{area}_")

    for ex in (r.get("extra_offers") or [])[:2]:
        if ex.get("pct"):
            extra = f"  ➕ {ex['pct']}% OFF"
            if ex.get("upto"): extra += f" up to ₹{ex['upto']}"
        elif ex.get("flat"):
            extra = f"  ➕ ₹{ex['flat']} OFF"
            if ex.get("min_order"): extra += f" above ₹{ex['min_order']}"
        elif ex.get("offer"):
            extra = f"  ➕ {ex['offer'][:60]}"
        else:
            continue
        if ex.get("coupon"):
            extra += f"  🎟️ `{ex['coupon']}`"
        lines.append(extra)

    return "\n".join(lines)


def _hot_deals_banner(offers: list, city_slug: str = "") -> str:
    """Top 3 highest-savings deals shown prominently at the top."""
    scored = sorted(offers, key=lambda r: -_deal_value(r))
    top    = [r for r in scored if _deal_value(r) >= 50][:3]
    if not top:
        return ""
    lines = ["━━━━━━━━━━━━━━━━━━━━━━",
             "🏆 *TOP DEALS — MAX SAVINGS*",
             "━━━━━━━━━━━━━━━━━━━━━━"]
    for i, r in enumerate(top, 1):
        url  = _rest_url(r, city_slug)
        name = f"[{r['name']}]({url})" if url else f"*{r['name']}*"
        demo = _deal_demo(r)
        badge = _badge(r)
        coupon = f"  🎟️ `{r['coupon']}`" if r.get("coupon") else ""
        lines.append(f"*#{i}* {name}{coupon}")
        if badge: lines.append(f"   {badge}")
        if demo:  lines.append(f"   {demo}")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _ts(): return datetime.now().strftime("%I:%M %p, %d %b")


def _city_slug(city_name: str) -> str:
    """City display name → URL slug (e.g. 'Navi Mumbai' → 'navi-mumbai')."""
    return re.sub(r'[^a-z0-9]+', '-', city_name.lower()).strip('-')


PER_PAGE = 10

def _page_kb(page: int, total: int, prefix: str) -> InlineKeyboardMarkup:
    """Prev / page-info / Next inline buttons."""
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page-1}"))
    buttons.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PER_PAGE < total:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page+1}"))
    return InlineKeyboardMarkup([buttons])


def build_offers_page(city_name, offers, total_scanned, page=0):
    """Grouped, real-only page: PERCENT OFF / FLAT OFF / COUPONS / OTHERS."""
    ts    = _ts()
    cslug = _city_slug(city_name)

    pct_list  = [r for r in offers if r.get("pct")]
    flat_list = [r for r in offers if not r.get("pct") and r.get("flat")]
    coup_list = [r for r in offers if not r.get("pct") and not r.get("flat") and r.get("coupon")]
    other_list= [r for r in offers if not r.get("pct") and not r.get("flat") and not r.get("coupon")]

    pct_list.sort(key=lambda r: -(r.get("pct") or 0))
    flat_list.sort(key=lambda r: -(r.get("flat") or 0))
    coup_list.sort(key=lambda r: -_deal_value(r))
    other_list.sort(key=lambda r: -_deal_value(r))

    sections = [
        ("🔥 PERCENT OFF DEALS", pct_list),
        ("💸 FLAT OFF DEALS",    flat_list),
        ("🎟️ COUPON CODES",      coup_list),
        ("🏷️ OTHER OFFERS",      other_list),
    ]
    flat = []
    for title, lst in sections:
        if lst:
            flat.append(("HEADER", title, len(lst)))
            for r in lst:
                flat.append(("ITEM", r, None))

    PER = 12
    total_items = sum(1 for x in flat if x[0] == "ITEM")
    total_pages = max(1, (total_items + PER - 1) // PER)

    out_lines = [
        f"🔥 *SWIGGY LIVE OFFERS — {city_name}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔍 {total_scanned} scan · ✅ {len(offers)} offers · ⏰ {ts}",
        f"📄 Page {page+1}/{total_pages}",
        "",
    ]

    item_idx = 0
    shown    = 0
    start    = page * PER
    end      = start + PER
    section_counter = {}
    current_section = None
    pending_header  = ""
    header_emitted  = True

    for entry in flat:
        if entry[0] == "HEADER":
            current_section = entry[1]
            section_counter[current_section] = 0
            pending_header = f"\n*{entry[1]} ({entry[2]})*\n"
            header_emitted = False
            continue
        if start <= item_idx < end:
            if not header_emitted:
                out_lines.append(pending_header)
                header_emitted = True
            section_counter[current_section] += 1
            out_lines.append(_card(section_counter[current_section], entry[1], cslug))
            out_lines.append("")
            shown += 1
        item_idx += 1
        if shown >= PER:
            break

    if shown == 0:
        out_lines.append("_Koi offer nahi mila._")
    else:
        out_lines.append("_Tap restaurant name → Swiggy pe open_")
    return "\n".join(out_lines)


def build_offers(city_name, offers, total):
    """Legacy single-page build (kept for search/top)."""
    return build_offers_page(city_name, offers, total, page=0)


def build_coupons(city_name, rests):
    ts = _ts()
    cslug = _city_slug(city_name)
    if not rests:
        return (f"🎟️ *COUPONS — {city_name}*\n"
                f"😕 Aaj koi coupon code nahi mila.\n"
                f"Sirf kuch restaurants hi codes dete hain — Offers try karo!\n_{ts}_")
    lines = [
        f"🎟️ *COUPON CODES — {city_name}*",
        f"✅ {len(rests)} coupon{'s' if len(rests)>1 else ''} mile!  ·  🕐 {ts}\n",
    ]
    for i, r in enumerate(rests[:25], 1):
        lines.append(_card(i, r, cslug))
        lines.append("")
    lines.append("_Copy code → Swiggy app → Checkout pe lagao_")
    return "\n".join(lines)


def build_top(city_name, deals, total):
    ts = _ts()
    cslug = _city_slug(city_name)
    if not deals:
        return f"🏆 *Best Deals — {city_name}*\n\n😕 Koi deal nahi mila.\n_{ts}_"
    lines = [
        f"🏆 *BEST DEALS — {city_name}*",
        f"Highest savings first  ·  {total} scanned  ·  🕐 {ts}\n",
    ]
    for i, r in enumerate(deals, 1):
        lines.append(_card(i, r, cslug))
        lines.append("")
    lines.append(f"_Tap name → Swiggy pe open hoga_")
    return "\n".join(lines)


def build_budget(city_name, budget, rests):
    ts = _ts()
    cslug = _city_slug(city_name)
    if not rests:
        return (f"💰 *Budget — {city_name}*\n"
                f"₹{budget}/person budget mein koi option nahi mila.\nThoda badha ke try karo!")
    lines = [
        f"💰 *BUDGET DEALS — {city_name}*",
        f"Budget: *₹{budget}/person*  ·  {len(rests)} options  ·  🕐 {ts}\n",
    ]
    for i, r in enumerate(rests[:15], 1):
        lines.append(_card(i, r, cslug))
        cost_line = f"~₹{r['cost_num']}/person"
        if r.get("saved"): cost_line += f"  →  Save *₹{r['saved']}*"
        lines.append(cost_line)
        lines.append("")
    lines.append(f"_Best savings pehle · {ts}_")
    return "\n".join(lines)


def build_search(q, city_name, matched, total):
    cslug = _city_slug(city_name)
    if not matched:
        return (f"🔍 *'{q}'* — {city_name}\n\n"
                f"{total} restaurants mein search kiya.\n"
                f"❌ Match nahi mila.")
    lines = [
        f"🔍 *'{q}'* — {city_name}",
        f"━━━━━━━━━━━━━━━━",
        f"✅ {len(matched)} result  ·  {total} restaurants scanned\n",
    ]
    for i, r in enumerate(matched, 1):
        lines.append(_card(i, r, cslug))
        if r["pct"] and r["cost_num"]:
            est = min(int(r["cost_num"] * r["pct"] / 100), r["upto"]) if r["upto"] else int(r["cost_num"] * r["pct"] / 100)
            if est: lines.append(f"💰 Save *₹{est}*")
        elif r["flat"]: lines.append(f"💰 Save *₹{r['flat']}*")
        lines.append("")
    return "\n".join(lines)


def build_split(city_name, amount, offers):
    lines = [
        f"✂️ *BILL SPLIT — {city_name}*",
        f"━━━━━━━━━━━━━━━━",
        f"Cart: *₹{amount}*\n",
        f"*Split:*",
        f"  2 log → ₹{amount//2} each",
        f"  3 log → ₹{amount//3} each  (₹{amount%3} leftover)",
        f"  4 log → ₹{amount//4} each  (₹{amount%4} leftover)\n",
        f"*Order Unlock:*",
    ]
    for val, msg in [(149,"Free delivery"),(199,"₹199 threshold"),(249,"40% OFF eligible"),
                     (299,"₹299 deals"),(399,"Premium coupons"),(499,"Super Saver")]:
        if amount >= val: lines.append(f"  ✅ {msg}")
        else:
            lines.append(f"  ➕ ₹{val-amount} aur → {msg}")
            break
    if offers:
        lines.append(f"\n*Best deals for ₹{amount}:*\n")
        for r in offers[:5]:
            badge = _badge(r)
            sv = 0
            if r["pct"]:
                raw = int(amount * r["pct"] / 100)
                sv = min(raw, r["upto"]) if r["upto"] else raw
            elif r["flat"] and (not r["min_order"] or amount >= r["min_order"]):
                sv = r["flat"]
            lines.append(f"• *{r['name']}*" + (f"  —  {badge}" if badge else ""))
            if sv: lines.append(f"  💰 Save ₹{sv} = Pay ₹{amount-sv}")
    lines.append(f"\n_Swiggy app pe order karo!_")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def kb(buttons, cols=2):
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([KeyboardButton(b) for b in buttons[i:i+cols]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_kb():
    rows = [
        [KeyboardButton("🔥 Offers Dekho"),    KeyboardButton("🏆 Best Deals")],
        [KeyboardButton("💰 Budget Filter"),   KeyboardButton("🔍 Restaurant Dhoondo")],
        [KeyboardButton("📮 Pin Code Offers"), KeyboardButton("📡 Share Location", request_location=True)],
        [KeyboardButton("📍 City Badlo"),      KeyboardButton("❓ Help")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def city_kb():
    rows = [[KeyboardButton(c) for c in POPULAR_CITIES[i:i+3]]
            for i in range(0, len(POPULAR_CITIES), 3)]
    rows.append([KeyboardButton("✏️ Koi aur city"), KeyboardButton("🔙 Back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def after_kb(city_name=""):
    rows = [
        [KeyboardButton("🔥 Offers"),          KeyboardButton("🏆 Best Deals")],
        [KeyboardButton("💰 Budget"),          KeyboardButton("🔍 Search")],
        [KeyboardButton("📍 City Badlo"),      KeyboardButton("🔄 Refresh")],
        [KeyboardButton("📡 Share Location", request_location=True), KeyboardButton("🏠 Home")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def budget_kb():
    return kb(["₹100","₹150","₹200","₹250","₹300","₹400","₹500","₹750",
               "✏️ Custom","🔙 Back"], cols=4)


def back_kb():
    return kb(["🔙 Back", "🏠 Home"], cols=2)


# ══════════════════════════════════════════════════════════════════════════════
#  CITY RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

def resolve(text: str):
    t = text.lower().strip()
    if t in CITIES: return t
    for k, v in CITIES.items():
        if v["display"].lower() == t: return k
    for k in CITIES:
        if k.startswith(t) or t in k: return k
    for k, v in CITIES.items():
        if t in v["display"].lower(): return k
    return None


def set_city(ctx, key: str):
    real = _resolve_city_key(key)
    cd = CITIES[real]
    # For ref cities we have the display; for the main entry we have points
    display = cd.get("display") or CITIES.get(real, {}).get("display", key.title())
    # store both resolved key and display
    ctx.user_data.update({"city_key": real, "city": display})


# ══════════════════════════════════════════════════════════════════════════════
#  FORCE JOIN + USER TRACKING + ADMIN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _track_user(update, context):
    """User ko _USERS mein record karo."""
    u = update.effective_user
    if not u: return
    uid  = u.id
    now  = time.time()
    if uid not in _USERS:
        _USERS[uid] = {
            "name":       u.first_name or "",
            "username":   u.username   or "",
            "first_seen": now,
            "last_seen":  now,
            "city":       "",
            "requests":   0,
        }
    _USERS[uid]["last_seen"] = now
    _USERS[uid]["requests"]  = _USERS[uid].get("requests", 0) + 1
    city = context.user_data.get("city", "")
    if city:
        _USERS[uid]["city"] = city


async def _not_joined_channels(bot, user_id: int) -> list:
    """Jinhe user ne join nahi kiya unka list return karo."""
    missing = []
    for ch in FORCE_CHANNELS:
        try:
            m = await bot.get_chat_member(ch["username"], user_id)
            if m.status in ("left", "kicked", "banned"):
                missing.append(ch)
        except Exception:
            missing.append(ch)   # error = assume not joined
    return missing


async def _show_join_wall(update) -> None:
    """Channel join karne ka message dikhao."""
    buttons = [
        [InlineKeyboardButton(f"📢 {ch['name']}", url=ch['url'])]
        for ch in FORCE_CHANNELS
    ]
    buttons.append([InlineKeyboardButton("✅ Maine Join Kar Liya!", callback_data="verify_join")])
    await update.message.reply_text(
        "⚠️ *Bot use karne ke liye pehle yeh channels join karo:*\n\n"
        "Join karne ke baad *✅ Maine Join Kar Liya!* dabao.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _ensure_joined(update, context) -> bool:
    """True = user ne sab join kiya; False = wall dikhaya, handler return kare."""
    uid = update.effective_user.id
    if uid == ADMIN_ID:           # admin ko force join ki zarurat nahi
        return True
    if uid in _VERIFIED:          # already verified this session
        return True
    missing = await _not_joined_channels(context.bot, uid)
    if not missing:
        _VERIFIED.add(uid)
        return True
    await _show_join_wall(update)
    return False


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    now      = time.time()
    total    = len(_USERS)
    act_1h   = sum(1 for u in _USERS.values() if now - u["last_seen"] < 3600)
    act_24h  = sum(1 for u in _USERS.values() if now - u["last_seen"] < 86400)
    cached   = len(_CACHE)
    total_req= sum(u.get("requests", 0) for u in _USERS.values())

    city_counts: dict = {}
    for u in _USERS.values():
        c = u.get("city", "")
        if c: city_counts[c] = city_counts.get(c, 0) + 1
    top_cities = sorted(city_counts.items(), key=lambda x: -x[1])[:10]
    cities_txt = "\n".join(f"  {c}: {n}" for c, n in top_cities) or "  (abhi koi nahi)"

    uptime_h = (now - _BOT_START_TS) / 3600
    text = (
        f"🔐 *Admin Panel — Swiggy Offer Bot*\n\n"
        f"👥 *Users:*\n"
        f"  Total registered: `{total}`\n"
        f"  Active (last 1h): `{act_1h}`\n"
        f"  Active (last 24h): `{act_24h}`\n\n"
        f"📊 *Stats:*\n"
        f"  Total requests: `{total_req}`\n"
        f"  Cached cities: `{cached}`\n"
        f"  Cache TTL: `{CACHE_TTL//60} min`\n"
        f"  Pool threads: `300`\n"
        f"  Uptime: `{uptime_h:.1f}h`\n\n"
        f"🏙️ *Top Cities (by users):*\n{cities_txt}\n\n"
        f"🔔 *Force Channels:*\n"
        + "\n".join(f"  • {ch['name']}" for ch in FORCE_CHANNELS)
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def on_verify_join_callback(update, context: ContextTypes.DEFAULT_TYPE):
    """User ne Join button dabaya — verify karo."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    missing = await _not_joined_channels(context.bot, uid)
    if not missing:
        _VERIFIED.add(uid)
        await query.edit_message_text(
            "✅ *Sab channels join kar liye!*\n\n"
            "Ab /start dabao aur offers dhundho 🍽️",
            parse_mode="Markdown",
        )
    else:
        names = ", ".join(ch["name"] for ch in missing)
        await query.answer(
            f"❌ Abhi bhi join nahi kiya: {names}\nPehle join karo phir verify karo.",
            show_alert=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  CORE RUNNERS
# ══════════════════════════════════════════════════════════════════════════════

async def _run_offers(update, context):
    city_key  = context.user_data["city_key"]
    city_name = context.user_data["city"]
    n_pts = len(CITIES.get(city_key, {}).get("points", []))
    msg = await update.message.reply_text(
        f"⏳ *{city_name}* ke offers scan ho rahe hain...\n"
        f"_{n_pts} locations simultaneously check kiye ja rahe hain!_",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    all_r  = await _async_get_all(city_key)
    offers = _srt([r for r in all_r if r["offer"]])
    await msg.delete()

    context.user_data["mode"]          = "offers"
    context.user_data["offers_list"]   = offers
    context.user_data["offers_total"]  = len(all_r)

    text    = build_offers_page(city_name, offers, len(all_r), page=0)
    page_kb = _page_kb(0, len(offers), "offpg") if len(offers) > PER_PAGE else None

    # Telegram max 4096 chars — safety truncation
    if len(text) > 4000:
        text = text[:3950] + "\n\n_...aur bhi hain, Next page dekho!_"

    sent = await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=page_kb if page_kb else after_kb(city_name)
    )
    context.user_data["offers_msg_id"] = sent.message_id
    return RESULTS


async def _run_top(update, context):
    city_key  = context.user_data["city_key"]
    city_name = context.user_data["city"]
    msg = await update.message.reply_text(
        f"⏳ *{city_name}* ke best deals dhundh rahe hain...",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    all_r = await _async_get_all(city_key)
    deals = _srt([r for r in all_r if r["offer"]])[:15]
    await msg.delete()
    await update.message.reply_text(
        build_top(city_name, deals, len(all_r)),
        parse_mode="Markdown", reply_markup=after_kb(city_name)
    )
    context.user_data["mode"] = "top"
    return RESULTS


async def _run_coupons(update, context):
    city_key  = context.user_data["city_key"]
    city_name = context.user_data["city"]
    msg = await update.message.reply_text(
        f"⏳ *{city_name}* ke coupon codes dhundh rahe hain...",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    all_r = await _async_get_all(city_key)
    rests = _srt([r for r in all_r if r["coupon"]])
    await msg.delete()
    await update.message.reply_text(
        build_coupons(city_name, rests),
        parse_mode="Markdown", reply_markup=after_kb(city_name)
    )
    context.user_data["mode"] = "coupons"
    return RESULTS


async def _run_budget(update, context, budget):
    city_key  = context.user_data["city_key"]
    city_name = context.user_data["city"]
    msg = await update.message.reply_text(
        f"⏳ ₹{budget}/person ke options dhundh rahe hain — {city_name}...",
        reply_markup=ReplyKeyboardRemove()
    )
    all_r = await _async_get_all(city_key)
    rests = _budget_filter(all_r, budget)
    await msg.delete()
    await update.message.reply_text(
        build_budget(city_name, budget, rests),
        parse_mode="Markdown", reply_markup=after_kb(city_name)
    )
    context.user_data["mode"] = "budget"
    context.user_data["last_budget"] = budget
    return RESULTS


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _track_user(update, context)
    # Force channel check
    if not await _ensure_joined(update, context):
        return MAIN_MENU   # wall shown, stay in current state

    name    = update.effective_user.first_name or "Dost"
    city    = context.user_data.get("city", "")
    city_ln = f"\n📍 *Current City:* {city}" if city else "\n📍 Pehle city chuno — *📍 City Badlo* dabao"
    await update.message.reply_text(
        f"🍽️ *Swiggy Offer Finder*\n\n"
        f"Namaste *{name}*! 👋{city_ln}\n\n"
        f"👇 Kya dhundhna hai?",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return MAIN_MENU


async def _need_city(update, context, mode):
    context.user_data["mode"] = mode
    await update.message.reply_text("📍 Apni city chuno:", reply_markup=city_kb())
    return PICK_CITY


async def on_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t    = update.message.text.strip()
    city = context.user_data.get("city_key")

    if t == "🔥 Offers Dekho":
        return await _run_offers(update, context) if city else await _need_city(update, context, "offers")
    if t == "🏆 Best Deals":
        return await _run_top(update, context) if city else await _need_city(update, context, "top")
    if t == "💰 Budget Filter":
        if city:
            await update.message.reply_text(
                f"💰 *Budget chuno — {context.user_data['city']}:*\n_(per person)_",
                parse_mode="Markdown", reply_markup=budget_kb()
            )
            return PICK_BUDGET
        return await _need_city(update, context, "budget")
    if t == "🔍 Restaurant Dhoondo":
        if city:
            await update.message.reply_text(
                f"🔍 *Restaurant naam type karo — {context.user_data['city']}:*",
                parse_mode="Markdown", reply_markup=back_kb()
            )
            return PICK_RESTAURANT
        return await _need_city(update, context, "search")
    if t == "📮 Pin Code Offers":
        await update.message.reply_text(
            "📮 *Pin Code se Offers Dhundho*\n\n"
            "Apna *6-digit pin code* type karo:\n"
            "_(e.g. 400001, 110001, 560001)_\n\n"
            "India ke kisi bhi area ka pin code daalo!",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return PICK_PINCODE
    if t == "📍 City Badlo":
        context.user_data["mode"] = "change_city"
        cur = context.user_data.get("city", "")
        txt = f"📍 *City Badlo*\nAbhi: *{cur}*\n\nNayi city chuno:" if cur else "📍 Apni city chuno:"
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=city_kb())
        return PICK_CITY
    if t == "❓ Help":
        return await _show_help(update, context)
    return await start(update, context)


async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("🔙 Back", "🏠 Home"):
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "✏️ Koi aur city":
        await update.message.reply_text(
            "✏️ City naam type karo:\n_(e.g. Surat, Meerut, Udaipur)_",
            parse_mode="Markdown", reply_markup=back_kb()
        )
        return PICK_CITY

    key = resolve(t)
    if not key:
        await update.message.reply_text(
            f"❌ *'{t}'* nahi mila. Dobara try karo.",
            parse_mode="Markdown", reply_markup=city_kb()
        )
        return PICK_CITY

    set_city(context, key)
    mode = context.user_data.get("mode", "offers")

    if mode == "change_city":
        city = context.user_data["city"]
        await update.message.reply_text(
            f"✅ City set: *{city}*\n\n👇 Kya dhundhna hai?",
            parse_mode="Markdown", reply_markup=main_kb()
        )
        return MAIN_MENU
    if mode == "offers":  return await _run_offers(update, context)
    if mode == "top":     return await _run_top(update, context)
    if mode == "coupons": return await _run_coupons(update, context)
    if mode == "search":
        await update.message.reply_text(
            f"🔍 Restaurant naam type karo — *{context.user_data['city']}:*",
            parse_mode="Markdown", reply_markup=back_kb()
        )
        return PICK_RESTAURANT
    if mode == "budget":
        await update.message.reply_text(
            f"💰 Budget chuno — *{context.user_data['city']}:*",
            parse_mode="Markdown", reply_markup=budget_kb()
        )
        return PICK_BUDGET
    if mode == "split":
        await update.message.reply_text(
            f"✂️ Cart amount type karo — *{context.user_data['city']}:*",
            parse_mode="Markdown", reply_markup=back_kb()
        )
        return PICK_SPLIT
    return await _run_offers(update, context)


# Main menu buttons — kisi bhi state mein ye text aaye toh on_main ko bhejo
_MENU_TEXTS = {
    "🔥 Offers Dekho", "🏆 Best Deals", "💰 Budget Filter",
    "🔍 Restaurant Dhoondo", "📮 Pin Code Offers", "📍 City Badlo",
    "❓ Help", "🏠 Home", "🔙 Back",
    "🔥 Offers", "🏆 Best Deals", "💰 Budget", "🔍 Search",
    "🔄 Refresh", "🔄 Naya Search",
}


async def on_pincode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne pin code type kiya — geocode karo aur offers fetch karo."""
    t = update.message.text.strip()
    _track_user(update, context)

    # Any menu button clicked → go back to main menu handler instantly
    if t in _MENU_TEXTS:
        return await on_main(update, context)

    pin = re.sub(r'\D', '', t)
    if len(pin) != 6:
        await update.message.reply_text(
            "❌ *6 digit ka valid pin code daalo.*\n_(e.g. 400001, 110001)_",
            parse_mode="Markdown", reply_markup=back_kb()
        )
        return PICK_PINCODE

    await update.message.reply_text(
        f"🔍 *Pin code {pin}* ka location dhundh raha hoon...",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )

    try:
        loop = asyncio.get_running_loop()
        lat, lng, area_full = await loop.run_in_executor(
            _FETCH_POOL, _pincode_to_coords, pin
        )

        if lat is None:
            await update.message.reply_text(
                f"❌ Pin code *{pin}* ka location nahi mila.\n\n"
                "🔍 *Kya karein?*\n"
                "• Pin code dobara check karo — 6 digits sahi hain?\n"
                "• Apni city manually select karo 👉 *📍 City Badlo*\n"
                "• Live GPS share karo 👉 *📡 Share Location*",
                parse_mode="Markdown",
                reply_markup=kb(["📮 Pin Code Offers", "📍 City Badlo", "🏠 Home"], cols=2)
            )
            return MAIN_MENU

        area_short = area_full.split(",")[0].strip() if area_full else pin

        await update.message.reply_text(
            f"📍 *{area_short}* mila! Swiggy offers fetch ho rahi hain...\n"
            f"_(~15-20 sec lagenge — 37+ scan points check ho rahe hain)_",
            parse_mode="Markdown"
        )

        all_rest = await _async_fetch_coords(lat, lng, pin)
        offers   = sorted(
            [r for r in all_rest if r["offer"] or r["coupon"]],
            key=lambda r: -r["score"]
        )

        if not offers:
            total_found = len(all_rest)
            if total_found > 0:
                msg = (
                    f"📍 *{area_short}* mein *{total_found} restaurants* mile,\n"
                    f"lekin abhi koi active offer nahi hai.\n\n"
                    "💡 *Try karo:*\n"
                    "• Thodi der baad dobara check karo\n"
                    "• Nearby area ka pin code try karo\n"
                    "• City select karke zyada offers dekho"
                )
            else:
                msg = (
                    f"😕 *{area_short}* (PIN: {pin}) ke aas-paas\n"
                    f"koi Swiggy restaurant nahi mila.\n\n"
                    "💡 *Swiggy yahan available nahi hai.*\n"
                    "Nearest city select karo ya doosra pin try karo."
                )
            await update.message.reply_text(
                msg, parse_mode="Markdown",
                reply_markup=kb(["📮 Pin Code Offers", "📍 City Badlo", "🔥 Offers Dekho", "🏠 Home"], cols=2)
            )
            return MAIN_MENU

        context.user_data["pin_offers"] = offers
        context.user_data["pin_total"]  = len(offers)
        context.user_data["pin_code"]   = pin
        context.user_data["pin_area"]   = area_short

        text    = _build_pincode_page(pin, area_short, offers, len(offers), page=0)
        page_kb = _page_kb(0, len(offers), "pinpg") if len(offers) > 8 else None
        after   = kb(["📮 Pin Code Offers", "🔥 Offers Dekho", "📍 City Badlo", "🏠 Home"], cols=2)

        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=page_kb if page_kb else after
        )

        # ── Top 5 ke REAL per-restaurant offers (Swiggy menu API) ──
        try:
            top = sorted(offers, key=lambda r: -_deal_value(r))[:5]
            tasks = []
            for r in top:
                rlat = r.get("lat") or lat
                rlng = r.get("lng") or lng
                tasks.append(async_fetch_restaurant_offers(r.get("rest_id", ""), rlat, rlng))
            real_lists = await asyncio.gather(*tasks, return_exceptions=True)
            def _has(x):
                if isinstance(x, Exception) or x is None: return False
                if isinstance(x, dict): return bool(x.get("offers") or x.get("prices"))
                return bool(x)
            if any(_has(x) for x in real_lists):
                await update.message.reply_text(
                    f"🎁 *Top {len(top)} restaurants ke REAL live offers + prices:*",
                    parse_mode="Markdown"
                )
            for r, real in zip(top, real_lists):
                if isinstance(real, Exception) or real is None:
                    real = {"offers": [], "prices": {}}
                if not _has(real):
                    continue
                card = _format_real_offers(r, real, "")
                try:
                    await update.message.reply_text(card, parse_mode="Markdown",
                                                    disable_web_page_preview=True)
                except Exception:
                    await update.message.reply_text(card, disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"pincode real offers error: {e}")

        if page_kb:
            await update.message.reply_text("👇", reply_markup=after)
        return MAIN_MENU

    except Exception as e:
        logger.error(f"on_pincode error: {e}")
        await update.message.reply_text(
            "⚠️ Kuch error aa gaya. /start dabao aur dobara try karo.",
            reply_markup=main_kb()
        )
        return MAIN_MENU


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne apni live GPS location share ki — wahan ke offers fetch karo."""
    _track_user(update, context)
    if not await _ensure_joined(update, context):
        return MAIN_MENU

    location = update.message.location
    lat, lng  = location.latitude, location.longitude

    await update.message.reply_text(
        "📡 *Location mili!* Aas-paas ke Swiggy offers dhundh raha hoon...\n"
        "_(~15-20 sec lagenge)_",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )

    try:
        all_rest = await _async_fetch_coords(lat, lng, "live-loc")
        offers   = sorted(
            [r for r in all_rest if r["offer"] or r["coupon"]],
            key=lambda r: -r["score"]
        )

        if not offers:
            total_found = len(all_rest)
            msg = (
                f"📍 Aapki location ke aas-paas *{total_found} restaurants* mile, "
                "lekin abhi koi active deal nahi hai.\n\nThodi der baad try karo."
            ) if total_found else (
                "😕 Aapki location ke aas-paas koi Swiggy restaurant nahi mila.\n\n"
                "📮 Pin code ya 📍 city try karo."
            )
            await update.message.reply_text(
                msg, parse_mode="Markdown", reply_markup=main_kb()
            )
            return MAIN_MENU

        context.user_data["pin_offers"] = offers
        context.user_data["pin_total"]  = len(offers)
        context.user_data["pin_code"]   = "📡"
        context.user_data["pin_area"]   = "Aapki Location"

        text    = _build_pincode_page("📡", "Aapki Location", offers, len(offers), page=0)
        page_kb = _page_kb(0, len(offers), "pinpg") if len(offers) > 8 else None
        after   = main_kb()

        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=page_kb if page_kb else after
        )

        # ── Top 5 ke REAL per-restaurant offers ──
        try:
            top = sorted(offers, key=lambda r: -_deal_value(r))[:5]
            tasks = []
            for r in top:
                rlat = r.get("lat") or lat
                rlng = r.get("lng") or lng
                tasks.append(async_fetch_restaurant_offers(r.get("rest_id", ""), rlat, rlng))
            real_lists = await asyncio.gather(*tasks, return_exceptions=True)
            def _has(x):
                if isinstance(x, Exception) or x is None: return False
                if isinstance(x, dict): return bool(x.get("offers") or x.get("prices"))
                return bool(x)
            if any(_has(x) for x in real_lists):
                await update.message.reply_text(
                    f"🎁 *Top {len(top)} restaurants ke REAL live offers + prices:*",
                    parse_mode="Markdown"
                )
            for r, real in zip(top, real_lists):
                if isinstance(real, Exception) or real is None:
                    real = {"offers": [], "prices": {}}
                if not _has(real):
                    continue
                card = _format_real_offers(r, real, "")
                try:
                    await update.message.reply_text(card, parse_mode="Markdown",
                                                    disable_web_page_preview=True)
                except Exception:
                    await update.message.reply_text(card, disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"location real offers error: {e}")

        if page_kb:
            await update.message.reply_text("👇", reply_markup=after)
        return MAIN_MENU

    except Exception as e:
        logger.error(f"on_location error: {e}")
        await update.message.reply_text(
            "⚠️ Kuch error aa gaya. Dobara try karo.", reply_markup=main_kb()
        )
        return MAIN_MENU


async def on_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t         = update.message.text.strip()
    city_name = context.user_data.get("city", "")

    if t in ("🏠 Home", "🔙 Back"):
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "◀️ Back":
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "🔥 Offers":                 return await _run_offers(update, context)
    if t == "🏆 Best Deals":             return await _run_top(update, context)
    if t == "🔄 Refresh":
        key = context.user_data.get("city_key")
        if key: _CACHE.pop(key, None)
        mode = context.user_data.get("mode", "offers")
        if mode == "top":    return await _run_top(update, context)
        if mode == "budget": return await _run_budget(update, context, context.user_data.get("last_budget", 300))
        return await _run_offers(update, context)
    if t == "💰 Budget":
        await update.message.reply_text(
            f"💰 Budget chuno — *{city_name}:*", parse_mode="Markdown", reply_markup=budget_kb()
        )
        return PICK_BUDGET
    if t in ("🔍 Search", "🔄 Naya Search"):
        await update.message.reply_text(
            f"🔍 Restaurant naam type karo — *{city_name}:*",
            parse_mode="Markdown", reply_markup=back_kb()
        )
        return PICK_RESTAURANT
    if t == "📍 City Badlo":
        context.user_data["mode"] = "change_city"
        cur = context.user_data.get("city", "")
        txt = f"📍 *City Badlo*\nAbhi: *{cur}*\n\nNayi city chuno:" if cur else "📍 Apni city chuno:"
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=city_kb())
        return PICK_CITY
    if t == "❓ Help":
        return await _show_help(update, context)
    await update.message.reply_text("👇", reply_markup=after_kb())
    return RESULTS


async def on_restaurant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🏠 Home":
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "🔙 Back":
        # Agar pehle results the, wapas results pe jaao
        if context.user_data.get("offers_list") is not None:
            await update.message.reply_text("👇", reply_markup=after_kb(context.user_data.get("city", "")))
            return RESULTS
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    city_key  = context.user_data.get("city_key")
    city_name = context.user_data.get("city", "")
    if not city_key:
        await update.message.reply_text("📍 City chuno:", reply_markup=city_kb())
        return PICK_CITY
    msg = await update.message.reply_text(
        f"🔍 *'{t}'* search ho raha hai — {city_name}...",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    matched, total = await async_search_rest(city_key, t)
    await msg.delete()
    if not matched:
        await update.message.reply_text(
            f"🔍 *'{t}'* — {city_name}\n\n"
            f"{total} restaurants scan kiye, ❌ match nahi mila.",
            parse_mode="Markdown",
            reply_markup=kb(["🔄 Naya Search", "🔥 Offers", "🔙 Back", "🏠 Home"], cols=2)
        )
        return RESULTS

    # Fetch REAL per-restaurant offers (Swiggy menu API) for top matches in parallel
    top = matched[:5]
    city_data = CITIES.get(city_key, {})
    if city_data.get("ref"):
        city_data = CITIES.get(city_data["ref"], {})
    default_pt = (city_data.get("points") or [(0.0, 0.0)])[0]

    tasks = []
    for r in top:
        lat = r.get("lat") or default_pt[0]
        lng = r.get("lng") or default_pt[1]
        tasks.append(async_fetch_restaurant_offers(r.get("rest_id", ""), lat, lng))
    real_lists = await asyncio.gather(*tasks, return_exceptions=True)

    intro = (f"🔍 *'{t}'* — {city_name}\n"
             f"✅ {len(matched)} match  ·  Top {len(top)} ke *REAL* offers:\n")
    await update.message.reply_text(intro, parse_mode="Markdown")

    cslug = _city_slug(city_name)
    for r, real in zip(top, real_lists):
        if isinstance(real, Exception) or real is None:
            real = []
        text = _format_real_offers(r, real, cslug)
        try:
            await update.message.reply_text(text, parse_mode="Markdown",
                                            disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"send card error: {e}")
            await update.message.reply_text(text, disable_web_page_preview=True)

    await update.message.reply_text(
        "👇 Aage kya?",
        reply_markup=kb(["🔄 Naya Search", "🔥 Offers", "🔙 Back", "🏠 Home"], cols=2)
    )
    return RESULTS


async def on_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🏠 Home":
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "🔙 Back":
        if context.user_data.get("offers_list") is not None:
            await update.message.reply_text("👇", reply_markup=after_kb(context.user_data.get("city", "")))
            return RESULTS
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    m = re.search(r'(\d+)', t.replace(',', ''))
    if not m:
        await update.message.reply_text("❌ Number type karo ya button dabao.", reply_markup=budget_kb())
        return PICK_BUDGET
    budget = int(m.group(1))
    if budget < 50 or budget > 5000:
        await update.message.reply_text("❌ ₹50–₹5000 ke beech amount chahiye.", reply_markup=budget_kb())
        return PICK_BUDGET
    return await _run_budget(update, context, budget)


async def on_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🏠 Home":
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    if t == "🔙 Back":
        if context.user_data.get("offers_list") is not None:
            await update.message.reply_text("👇", reply_markup=after_kb(context.user_data.get("city", "")))
            return RESULTS
        await update.message.reply_text("Main menu:", reply_markup=main_kb())
        return MAIN_MENU
    m = re.search(r'(\d+)', t.replace(',', ''))
    if not m:
        await update.message.reply_text("❌ Amount type karo, e.g. `600`", reply_markup=back_kb())
        return PICK_SPLIT
    amount = int(m.group(1))
    city_key  = context.user_data["city_key"]
    city_name = context.user_data["city"]
    offers = get_offers(city_key)
    await update.message.reply_text(
        build_split(city_name, amount, offers),
        parse_mode="Markdown",
        reply_markup=kb(["✂️ Naya Amount", "🔥 Offers", "🏠 Home"], cols=3)
    )
    return RESULTS


# ── Slash commands ─────────────────────────────────────────────────────────────

async def cmd_start(u, c): return await start(u, c)

async def cmd_offers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/offers pune`", parse_mode="Markdown"); return MAIN_MENU
    key = resolve(" ".join(context.args))
    if not key: await update.message.reply_text("❌ City nahi mila."); return MAIN_MENU
    set_city(context, key); return await _run_offers(update, context)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/top pune`", parse_mode="Markdown"); return MAIN_MENU
    key = resolve(" ".join(context.args))
    if not key: await update.message.reply_text("❌ City nahi mila."); return MAIN_MENU
    set_city(context, key); return await _run_top(update, context)

async def cmd_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/coupons pune`", parse_mode="Markdown"); return MAIN_MENU
    key = resolve(" ".join(context.args))
    if not key: await update.message.reply_text("❌ City nahi mila."); return MAIN_MENU
    set_city(context, key); return await _run_coupons(update, context)

async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/budget 300 pune`", parse_mode="Markdown"); return MAIN_MENU
    try: amount = int(args[0])
    except: await update.message.reply_text("❌ Pehle amount."); return MAIN_MENU
    key = resolve(" ".join(args[1:]))
    if not key: await update.message.reply_text("❌ City nahi mila."); return MAIN_MENU
    set_city(context, key); return await _run_budget(update, context, amount)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/search pune dominos`", parse_mode="Markdown"); return MAIN_MENU
    key = resolve(args[0])
    if not key: await update.message.reply_text("❌ City nahi mila."); return MAIN_MENU
    set_city(context, key)
    brand = " ".join(args[1:])
    msg = await update.message.reply_text(f"🔍 Searching '{brand}'...")
    matched, total = search_rest(context.user_data["city_key"], brand)
    await msg.delete()
    await update.message.reply_text(
        build_search(brand, context.user_data["city"], matched, total),
        parse_mode="Markdown", reply_markup=after_kb(context.user_data["city"])
    )
    return RESULTS

async def _show_help(update, context):
    city = context.user_data.get("city", "")
    city_line = f"📍 Current City: *{city}*\n\n" if city else ""
    text = (
        f"❓ *SWIGGY OFFER FINDER — GUIDE*\n\n"
        f"{city_line}"
        f"*🚀 Shuru Kaise Karein?*\n"
        f"1️⃣ *📍 City Badlo* → Apni city set karo\n"
        f"2️⃣ *🔥 Offers Dekho* → Saare active offers dekho\n"
        f"3️⃣ Restaurant ka naam *tap karo* → Swiggy app mein khulega\n"
        f"4️⃣ Order karo aur discount enjoy karo! 🎉\n\n"
        f"*💸 Paise Kaise Bachayein?*\n\n"
        f"🔥 *Offers Dekho*\n"
        f"Saari active % OFF aur Flat OFF deals\n"
        f"50+ offers hain to *Next ➡️* button se aage dekho\n\n"
        f"🏆 *Best Deals*\n"
        f"Sabse zyada discount wale restaurants pehle dikhenge\n"
        f"Maximum saving ke liye yahan se order karo\n\n"
        f"💰 *Budget Filter*\n"
        f"Apna per-person budget set karo\n"
        f"Sirf wahi options dikhenge jo afford kar sako\n\n"
        f"🔍 *Restaurant Dhoondo*\n"
        f"Kisi specific brand ka offer dhundho\n"
        f"e.g. Dominos, KFC, McDonald's, Barbeque\n\n"
        f"📍 *City Badlo*\n"
        f"Ghar, office ya bahar — kisi bhi city ke offers dekho\n"
        f"City badalne ke baad menu wapas aata hai\n\n"
        f"🔄 *Refresh*\n"
        f"Fresh offers load karo (10 min cache reset)\n\n"
        f"*📋 Order Karte Waqt Dhyan Rakho:*\n"
        f"✅ *Min ₹XXX* — Is se kam order pe discount nahi milega\n"
        f"✅ *Upto ₹XXX* — Maximum yahi discount milega\n"
        f"✅ Coupon code hoga toh copy karo → Checkout mein *Apply Coupon* mein lagao\n"
        f"✅ Restaurant tap karo → Swiggy app mein directly open hoga"
    )
    kb_to_use = after_kb() if context.user_data.get("city_key") else main_kb()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_to_use)
    return RESULTS if context.user_data.get("city_key") else MAIN_MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _show_help(update, context)


# ══════════════════════════════════════════════════════════════════════════════
#  PAGINATION CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

async def on_page_callback(update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Next/Prev button presses for offer pagination (city + pincode)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "noop":
        return

    # ── Pin code pagination ──────────────────────────────────────────────────
    if data.startswith("pinpg:"):
        try:
            page = int(data.split(":")[1])
        except (IndexError, ValueError):
            return
        offers    = context.user_data.get("pin_offers", [])
        pin       = context.user_data.get("pin_code", "")
        area      = context.user_data.get("pin_area", "")
        if not offers:
            await query.answer("Session expired. /start se dobara try karo.")
            return
        text    = _build_pincode_page(pin, area, offers, len(offers), page=page)
        page_kb = _page_kb(page, len(offers), "pinpg")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=page_kb)
        return

    # ── City offers pagination ───────────────────────────────────────────────
    if not data.startswith("offpg:"):
        return

    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        return

    offers     = context.user_data.get("offers_list", [])
    total      = context.user_data.get("offers_total", 0)
    city_name  = context.user_data.get("city", "")

    if not offers or not city_name:
        await query.answer("Session expired. /start se dobara try karo.")
        return

    text    = build_offers_page(city_name, offers, total, page=page)
    page_kb = _page_kb(page, len(offers), "offpg")

    if len(text) > 4000:
        text = text[:3950] + "\n\n_...aur bhi hain, Next page dekho!_"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=page_kb)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def _background_cache_warmer():
    """Popular cities ko background mein pre-warm karta hai.
    Bot start hone ke 5 sec baad shuru hota hai, har 8 min mein refresh.
    Isse pehle user ki request slow nahi hogi — cache ready milega!"""
    await asyncio.sleep(5)  # bot setup complete hone do
    warm_cities = [
        "delhi", "mumbai", "bangalore", "hyderabad", "chennai",
        "kolkata", "pune", "ahmedabad", "jaipur", "lucknow",
        "noida", "gurgaon", "chandigarh", "kochi", "indore",
    ]
    loop = asyncio.get_running_loop()
    while True:
        logger.info("🔥 Background cache warmer: starting...")
        for city_key in warm_cities:
            try:
                key = _resolve_city_key(city_key)
                cached = _CACHE.get(key)
                now = time.time()
                # Sirf refresh karo agar cache expire ho raha ho (2 min remaining)
                if not cached or (now - cached["ts"]) > (CACHE_TTL - 120):
                    await loop.run_in_executor(_FETCH_POOL, _fetch_raw, city_key)
                    await asyncio.sleep(2)  # Swiggy ko breathe karne do
            except Exception as e:
                logger.debug(f"Cache warmer error {city_key}: {e}")
        logger.info("✅ Background cache warmer: cycle complete")
        await asyncio.sleep(480)  # 8 min baad phir


async def _post_init(app):
    """Bot initialize hone ke baad background warmer start karo.
    job_queue ki zaroorat nahi — pure asyncio.create_task use karo."""
    asyncio.create_task(_background_cache_warmer())
    logger.info("✅ Background cache warmer task created")


async def _rate_limit_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Per-user rate limiter — ek user bot ko spam na kar sake."""
    if update.effective_user:
        uid = update.effective_user.id
        if not _check_rate_limit(uid):
            if update.message:
                await update.message.reply_text("⏳ Thoda ruko... 1-2 second mein dobara try karo.")
            return False  # drop karo
    return True


def main():
    print("🚀 Swiggy Offer Finder Bot starting...")

    # ── HIGH-PERFORMANCE Application builder ─────────────────────────────────
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(512)          # 512 users simultaneously handle karo
        .connection_pool_size(512)        # Telegram API connections pool
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .pool_timeout(10)
        .post_init(_post_init)            # Background warmer — NO job_queue needed!
        .build()
    )

    entry = [
        CommandHandler("start",   cmd_start),
        CommandHandler("help",    cmd_help),
        CommandHandler("offers",  cmd_offers),
        CommandHandler("top",     cmd_top),
        CommandHandler("coupons", cmd_coupons),
        CommandHandler("budget",  cmd_budget),
        CommandHandler("search",  cmd_search),
    ]

    loc_handler = MessageHandler(filters.LOCATION, on_location)

    conv = ConversationHandler(
        entry_points=entry + [loc_handler],
        states={
            MAIN_MENU:       [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_main)],
            PICK_CITY:       [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_city)],
            PICK_RESTAURANT: [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_restaurant)],
            PICK_BUDGET:     [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_budget)],
            PICK_SPLIT:      [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_split)],
            RESULTS:         [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_result)],
            PICK_PINCODE:    [loc_handler, MessageHandler(filters.TEXT & ~filters.COMMAND, on_pincode)],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("help",  cmd_help),
            loc_handler,
            MessageHandler(filters.Regex("^🏠 Home$"), start),
            MessageHandler(filters.LOCATION, on_location),  # global fallback for location
        ],
        allow_reentry=True,
        conversation_timeout=1800,  # 30 min idle timeout — memory save
    )

    app.add_handler(conv)
    # ── GLOBAL fallback for location — works even if conv state is wrong ──────
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(CallbackQueryHandler(on_page_callback,      pattern=r"^offpg:|^pinpg:|^noop$"))
    app.add_handler(CallbackQueryHandler(on_verify_join_callback, pattern=r"^verify_join$"))
    app.add_handler(CommandHandler("admin", cmd_admin))
    for cmd, fn in [("offers",cmd_offers),("top",cmd_top),("coupons",cmd_coupons),
                    ("budget",cmd_budget),("search",cmd_search)]:
        app.add_handler(CommandHandler(cmd, fn))

    n = len(set(v.get("display", k) for k, v in CITIES.items() if "points" in v))
    print(f"✅ Bot ready! {n}+ cities | Multi-location scan | 3000+ users support!")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # restart pe purane queued msgs skip karo
    )


if __name__ == "__main__":
    main()
