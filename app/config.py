import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


QUOTEX_EMAIL = os.getenv("QUOTEX_EMAIL", "")
QUOTEX_PASSWORD = os.getenv("QUOTEX_PASSWORD", "")
QUOTEX_ACCOUNT_MODE = os.getenv("QUOTEX_ACCOUNT_MODE", "PRACTICE").upper()
QUOTEX_LANG = os.getenv("QUOTEX_LANG", "en")

# Optional: reuse a session captured from a real browser login (SSID token +
# cookie header) instead of doing a fresh email/password login every start.
# When QUOTEX_SESSION_TOKEN is set, pyquotex skips the Cloudflare-guarded
# HTTP login page entirely and connects the websocket directly with it.
QUOTEX_SESSION_TOKEN = os.getenv("QUOTEX_SESSION_TOKEN", "")
QUOTEX_SESSION_COOKIES = os.getenv("QUOTEX_SESSION_COOKIES", "")
QUOTEX_USER_AGENT = os.getenv("QUOTEX_USER_AGENT", "")
# After this many consecutive full-attempt failures using the session
# token, quotex_client.py stops trusting it and falls back to a fresh
# email/password login automatically (see quotex_client.auth_mode()).
QUOTEX_SESSION_FAILURE_THRESHOLD = int(os.getenv("QUOTEX_SESSION_FAILURE_THRESHOLD", "3"))

# A failed email/password login must NOT be retried on the same tight
# loop as a flaky-network failure: every attempt hits the
# Cloudflare-guarded sign-in page, and from Railway's shared egress IPs a
# fast retry loop is exactly what gets the app blocked. So each
# consecutive password-login failure doubles the wait before the next
# attempt, starting at the base and capping at the max.
LOGIN_BACKOFF_BASE_SECONDS = _int("LOGIN_BACKOFF_BASE_SECONDS", 300)
LOGIN_BACKOFF_MAX_SECONDS = _int("LOGIN_BACKOFF_MAX_SECONDS", 3600)

# --- Railway self-persistence -------------------------------------------
# When a password login succeeds, the freshly captured session is written
# back into this service's own Railway variables so the next boot reuses
# the token instead of re-triggering the Cloudflare-guarded login page.
# See app/railway_control.py. Without RAILWAY_API_TOKEN the app still
# works, it just loses the token on redeploy.
RAILWAY_API_TOKEN = os.getenv("RAILWAY_API_TOKEN", "")
# "account" (personal/team token -> Authorization: Bearer) or "project"
# (project-scoped token -> Project-Access-Token header). Project tokens
# are read-only on Railway's public API, so writing variables needs an
# account/team token.
RAILWAY_TOKEN_TYPE = os.getenv("RAILWAY_TOKEN_TYPE", "account").strip().lower()
RAILWAY_API_URL = os.getenv("RAILWAY_API_URL", "https://backboard.railway.com/graphql/v2")
# Railway injects these three into every deployment — they only need to
# be set by hand when running this code somewhere else.
RAILWAY_PROJECT_ID = os.getenv("RAILWAY_PROJECT_ID", "")
RAILWAY_ENVIRONMENT_ID = os.getenv("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID", "")
# Redeploy after persisting so a clean boot proves the stored token
# works end-to-end. Railway may already redeploy on a variable change;
# set this to 0 if you'd rather not stack a second deploy on top.
RAILWAY_REDEPLOY_AFTER_LOGIN = _bool("RAILWAY_REDEPLOY_AFTER_LOGIN", True)

# Required passcode to hit POST /api/session (paste a fresh token from the
# frontend without redeploying). The app is publicly reachable once
# deployed, so this endpoint must not be left open to anyone who finds
# the URL — if ADMIN_TOKEN is unset, /api/session refuses every request.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

MIN_CONFIDENCE = _float("MIN_CONFIDENCE", 0.6)
ALWAYS_SIGNAL = _bool("ALWAYS_SIGNAL", True)
SIGNAL_COOLDOWN_SECONDS = _int("SIGNAL_COOLDOWN_SECONDS", 60)
MIN_CONFIRMATIONS = _int("MIN_CONFIRMATIONS", 2)
QUALITY_FLOOR = _float("QUALITY_FLOOR", 0.5)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 8000)

CANDLE_PERIOD_SECONDS = 60

DB_PATH = Path(os.getenv("DB_PATH")) if os.getenv("DB_PATH") else ROOT_DIR / "candles.db"
SESSION_ROOT = ROOT_DIR  # pyquotex stores session.json here

# display_name -> candidate Quotex asset codes, in preference order.
# Resolved against the live get_all_assets() result at startup since
# Quotex sometimes varies OTC suffix casing.
OTC_PAIRS: dict[str, list[str]] = {
    "USD/BRL OTC": ["USDBRL_otc", "BRLUSD_otc"],
    "USD/INR OTC": ["USDINR_otc"],
    "USD/IDR OTC": ["USDIDR_otc"],
    "USD/COP OTC": ["USDCOP_otc"],
    "USD/BDT OTC": ["USDBDT_otc"],
    "USD/MXN OTC": ["USDMXN_otc"],
    "NZD/USD OTC": ["NZDUSD_otc"],
    "USD/DZD OTC": ["USDDZD_otc"],
    "USD/PHP OTC": ["USDPHP_otc"],
    "USD/PKR OTC": ["USDPKR_otc"],
    "USD/ZAR OTC": ["USDZAR_otc"],
}

FOREX_PAIRS: dict[str, list[str]] = {
    "AUD/USD": ["AUDUSD"],
    "EUR/USD": ["EURUSD"],
    "USD/JPY": ["USDJPY"],
    "EUR/GBP": ["EURGBP"],
    "GBP/USD": ["GBPUSD"],
}

ALL_PAIRS: dict[str, list[str]] = {**OTC_PAIRS, **FOREX_PAIRS}

# Forex pairs are kept streaming at all times (subject to market hours);
# OTC pairs stream on demand when a viewer requests them.
ALWAYS_ON_DISPLAY_NAMES = list(FOREX_PAIRS.keys())
