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


# --- Quotex credentials (used only for the one-shot email/password
#     login; the running app authenticates against the pasted token) ---
QUOTEX_EMAIL = os.getenv("QUOTEX_EMAIL", "")
QUOTEX_PASSWORD = os.getenv("QUOTEX_PASSWORD", "")
QUOTEX_ACCOUNT_MODE = os.getenv("QUOTEX_ACCOUNT_MODE", "PRACTICE").upper()
QUOTEX_LANG = os.getenv("QUOTEX_LANG", "en")

# --- Token-only auth surface ---
# The app connects to Quotex via the SSID token pasted into the Settings
# tab (POST /api/session). The token is the only auth credential the
# frontend ever sees — no admin passcode, no API keys, no Railway tokens
# are exposed or required. When a token is set, pyquotex skips the
# Cloudflare-guarded HTTP login page entirely and connects the websocket
# directly with it.
QUOTEX_SESSION_TOKEN = os.getenv("QUOTEX_SESSION_TOKEN", "")
QUOTEX_SESSION_COOKIES = os.getenv("QUOTEX_SESSION_COOKIES", "")
QUOTEX_USER_AGENT = os.getenv("QUOTEX_USER_AGENT", "")

# Optional HTTP(S) proxy used *only* for the one-shot email/password
# login, e.g. "http://user:pass@host:port". This is the escape hatch
# for an egress IP Cloudflare has already blocked: the login goes out
# through the proxy while the websocket keeps using the service's own
# connection. With retries removed, the proxy is the one chance a
# password login has, so it's worth setting if credentials are used.
QUOTEX_LOGIN_PROXY = os.getenv("QUOTEX_LOGIN_PROXY", "")

# A session token can die mid-run — the websocket simply stops
# delivering ticks. The watchdog notices that silence and rebuilds the
# connection from the pasted token. With the password-login retry loop
# removed, the watchdog only ever reuses the token; it never falls back
# to a fresh login on its own.
CONNECTION_WATCHDOG_SECONDS = _int("CONNECTION_WATCHDOG_SECONDS", 60)
STALE_FEED_SECONDS = _int("STALE_FEED_SECONDS", 300)

MIN_CONFIDENCE = _float("MIN_CONFIDENCE", 0.6)
ALWAYS_SIGNAL = _bool("ALWAYS_SIGNAL", True)
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

# Per-pair Quotex payout (fraction, e.g. 0.85 = 85%). Used by backtest.py
# to compute the breakeven win rate (p_be = 1 / (1 + payout)) and the
# payout-adjusted ROI per trade. These are conservative defaults based
# on typical Quotex OTC payouts observed at 70-92% — override per pair
# from the broker's payout table as needed.
PAIR_PAYOUT: dict[str, float] = {
    # OTC exotic pairs typically have higher payouts (more broker edge)
    "USD/BRL OTC": 0.92,
    "USD/INR OTC": 0.90,
    "USD/IDR OTC": 0.92,
    "USD/COP OTC": 0.90,
    "USD/BDT OTC": 0.92,
    "USD/MXN OTC": 0.88,
    "NZD/USD OTC": 0.85,
    "USD/DZD OTC": 0.90,
    "USD/PHP OTC": 0.88,
    "USD/PKR OTC": 0.90,
    "USD/ZAR OTC": 0.88,
    # Major forex pairs have lower payouts (less broker edge)
    "AUD/USD": 0.70,
    "EUR/USD": 0.72,
    "USD/JPY": 0.72,
    "EUR/GBP": 0.70,
    "GBP/USD": 0.72,
}
