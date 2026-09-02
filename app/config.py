import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


QUOTEX_ACCOUNT_MODE = os.getenv("QUOTEX_ACCOUNT_MODE", "PRACTICE").upper()
QUOTEX_LANG = os.getenv("QUOTEX_LANG", "en")

# --- Token-only auth surface ---
# The app connects to Quotex via the SSID token pasted into the Settings
# tab (POST /api/session) — the ONLY credential the frontend ever asks
# for. There is no email/password login path at all: no Cloudflare-guarded
# login page is ever touched, no admin passcode, no API keys. Without a
# token, the app simply waits for one to be pasted in.
QUOTEX_SESSION_TOKEN = os.getenv("QUOTEX_SESSION_TOKEN", "")
QUOTEX_SESSION_COOKIES = os.getenv("QUOTEX_SESSION_COOKIES", "")
QUOTEX_USER_AGENT = os.getenv("QUOTEX_USER_AGENT", "")

# A session token can die mid-run — the websocket simply stops
# delivering ticks. The watchdog notices that silence and rebuilds the
# connection by reusing the pasted token; there is no password path to
# fall back to.
CONNECTION_WATCHDOG_SECONDS = _int("CONNECTION_WATCHDOG_SECONDS", 60)
STALE_FEED_SECONDS = _int("STALE_FEED_SECONDS", 300)

MIN_CONFIDENCE = _float("MIN_CONFIDENCE", 0.65)

# 2026-09 (confluence v3) — the engine fires a signal ONLY when several
# independent strategy families agree on the direction. "MIN_CONFIRMATIONS"
# counted correlated detectors as separate confirmations; a family is the
# honest unit (one idea, one vote — see decision.py's cluster cap). The old
# default of 1 let a single family confirm a signal, which is the single
# largest producer of wrong calls. The legacy env name is still honored.
MIN_CONFLUENCE_STRATEGIES = _int("MIN_CONFLUENCE_STRATEGIES", _int("MIN_CONFIRMATIONS", 2))
QUALITY_FLOOR = _float("QUALITY_FLOOR", 0.65)  # winning-side share of total vote weight

# A signal may not fight the market's own character: when the regime read
# is at least this strong (trend or range), the winning side must include
# at least one strategy family whose theory MATCHES that regime.
MIN_REGIME_ALIGNMENT_STRENGTH = _float("MIN_REGIME_ALIGNMENT_STRENGTH", 0.30)

# Until a confluence (signature) has enough graded history on a pair to
# state a measured confidence, it may only fire when the structural
# agreement is far above the ordinary gate — the bootstrap earns trust
# slowly, and once the record exists the measured gate (MIN_CONFIDENCE)
# takes over completely.
BOOTSTRAP_AGREEMENT = _float("BOOTSTRAP_AGREEMENT", 0.75)

# The engine needs this many clean closed candles before any signal can
# fire — below that every indicator/pattern/regime read is degraded and
# the honest answer is silence.
MIN_HISTORY_CANDLES = _int("MIN_HISTORY_CANDLES", 80)

# A candle finalized this many seconds past its boundary would put the
# follower into a market whose entry minute already started — the recorded
# entry price no longer matches anything a human could have entered at.
# (The old guard allowed a full 60s of lateness, so "history" could grade
# trades nobody actually traded.) Beyond this window: no signal.
SIGNAL_MAX_LATE_SECONDS = _int("SIGNAL_MAX_LATE_SECONDS", 5)

# Signals fire only on qualified multi-strategy confluence — a fraction of
# candles per pair (typically well under 10%). Volume is bounded anyway:
# OTC pairs are live ~22h/day, so 16 pairs still produce thousands of
# graded rows per day at the peak. Left
# unbounded this both grows the Railway volume indefinitely and makes
# /api/patterns and /api/winrate (full-table scans) slower every day.
# pattern_stats (the compact aggregate weights.py actually learns from)
# is never pruned — only the raw per-candle/per-signal log rows are, so
# pruning does not erase what a source has learned. PENDING signals are
# never pruned regardless of age; only rows already graded (WIN/LOSS/
# DRAW) or plain candle history count against the window.
CANDLE_RETENTION_DAYS = _int("CANDLE_RETENTION_DAYS", 30)
SIGNAL_RETENTION_DAYS = _int("SIGNAL_RETENTION_DAYS", 60)
PRUNE_INTERVAL_SECONDS = _int("PRUNE_INTERVAL_SECONDS", 3600)

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
