import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import backtest, config, db, patterns as patterns_module, quotex_client, railway_control
from app.evaluator import run_evaluator
from app.feed import FeedManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app_state: dict = {"quotex_connected": False, "error": None, "pairs": list(config.ALL_PAIRS.keys())}
_ws_clients: set[WebSocket] = set()


async def broadcast(message: dict) -> None:
    if not _ws_clients:
        return
    payload = json.dumps(message, default=str)
    dead = []
    for ws in list(_ws_clients):  # snapshot: _ws_clients can mutate mid-iteration as clients (re)connect
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _on_candle(pair: str, candle: dict, is_final: bool) -> None:
    await broadcast({"type": "candle", "pair": pair, "candle": candle, "final": is_final})


async def _on_signal(pair: str, signal: dict) -> None:
    await broadcast({"type": "signal", "pair": pair, "signal": signal})


async def _on_graded(signal: dict) -> None:
    await broadcast({"type": "graded", "pair": signal["pair"], "signal": signal})


RETRY_BACKOFF_SECONDS = 20


async def _bootstrap_feed() -> None:
    """Keeps retrying the Quotex connection forever. On this network,
    handshake failures are transient (flaky tethered link) rather than
    permanent auth/config problems, so a one-shot failure here shouldn't
    stop the app from ever coming online — it should just keep trying."""
    attempt = 0
    while True:
        attempt += 1
        try:
            manager = FeedManager(on_candle=_on_candle, on_signal=_on_signal)
            await manager.start()
            app_state["quotex_connected"] = True
            app_state["error"] = None
            app_state["feed_manager"] = manager
            logger.info("Feed manager started for %d pairs (attempt %d)", len(manager.pairs), attempt)
            return
        except quotex_client.LoginBackoffError as e:
            # Expected, self-clearing state rather than a fault: the app is
            # deliberately sitting out the Cloudflare-avoiding login backoff.
            app_state["quotex_connected"] = False
            app_state["error"] = str(e)
            delay = quotex_client.retry_delay_seconds(RETRY_BACKOFF_SECONDS)
            logger.info("Waiting %ds before the next login attempt (attempt %d): %s", delay, attempt, e)
            await asyncio.sleep(delay)
        except Exception as e:
            app_state["quotex_connected"] = False
            app_state["error"] = str(e)
            # A failed password login sets a backoff far longer than
            # RETRY_BACKOFF_SECONDS — retrying sooner would just hammer the
            # Cloudflare-guarded login page again.
            delay = quotex_client.retry_delay_seconds(RETRY_BACKOFF_SECONDS)
            logger.exception("Failed to start Quotex feed (attempt %d), retrying in %ds", attempt, delay)
            await asyncio.sleep(delay)


async def _restart_feed() -> None:
    """Tears down whatever's currently running (old bootstrap retry loop,
    old feed manager, old Quotex client) and starts fresh — used after a
    new session token is pasted in via /api/session so it takes effect
    immediately instead of waiting for the next natural reconnect."""
    old_bootstrap_task = app_state.get("bootstrap_task")
    if old_bootstrap_task is not None:
        old_bootstrap_task.cancel()
        try:
            await old_bootstrap_task
        except (asyncio.CancelledError, Exception):
            pass

    old_manager = app_state.get("feed_manager")
    if old_manager is not None:
        await old_manager.stop()
        app_state["feed_manager"] = None

    await quotex_client.close_client()

    app_state["quotex_connected"] = False
    app_state["error"] = None
    app_state["bootstrap_task"] = asyncio.create_task(_bootstrap_feed())


async def _connection_watchdog() -> None:
    """Rebuilds the connection when the feed goes silent.

    A session token that expires mid-run doesn't announce itself: no
    exception, no disconnect callback, the websocket just stops
    delivering ticks. Nothing else in the app would ever notice, so the
    app would sit there "connected" and signal-less until someone
    restarted it. OTC pairs trade around the clock, so silence across
    *every* pair means the connection is gone, not that the market is
    closed. The restart goes through get_client(), which tries the token
    first and only falls back to a password login (under its backoff) if
    the token really is dead.
    """
    while True:
        await asyncio.sleep(config.CONNECTION_WATCHDOG_SECONDS)
        manager = app_state.get("feed_manager")
        if manager is None:
            continue  # the bootstrap loop already owns the reconnect

        stale_for = manager.seconds_since_last_tick()
        connected = await quotex_client.is_connected()
        if connected and stale_for < config.STALE_FEED_SECONDS:
            continue

        logger.warning(
            "Connection looks dead (authenticated=%s, no ticks for %.0fs) — rebuilding it",
            connected,
            stale_for,
        )
        app_state["reconnects"] = app_state.get("reconnects", 0) + 1
        await _restart_feed()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    # Must run before the first connect: it restores the login backoff a
    # previous boot was serving (Railway restarts would otherwise reset
    # it) and any session token captured earlier.
    await quotex_client.load_persisted_state()
    app_state["bootstrap_task"] = asyncio.create_task(_bootstrap_feed())
    asyncio.create_task(run_evaluator(_on_graded))
    asyncio.create_task(_connection_watchdog())
    yield


app = FastAPI(lifespan=lifespan)

# 2026-08 — Open public API.
# Per the user requirement: "call put data signals যেনো অ্যাপ ইউআরএল
# ব্যবহার করে, যে কেউ ফিচ করতে পারে। একেবারে ওপেন থাকবে।"
# All GET /api/* read endpoints (signals, candles, history, backtest,
# patterns, winrate) are intentionally left open without auth. The only
# mutation endpoint (/api/session) still requires ADMIN_TOKEN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/status")
async def status():
    return {
        "quotex_connected": app_state["quotex_connected"],
        "error": app_state["error"],
        "pairs": app_state["pairs"],
        "active_pairs": list(app_state.get("feed_manager").pairs.keys()) if app_state.get("feed_manager") else [],
        "min_confidence": config.MIN_CONFIDENCE,
        "always_signal": config.ALWAYS_SIGNAL,
        "account_mode": config.QUOTEX_ACCOUNT_MODE,
        "auth_mode": quotex_client.auth_mode(),
        "password_failure_count": quotex_client.password_failure_count(),
        "login_backoff_seconds_remaining": quotex_client.login_backoff_seconds_remaining(),
        "last_login_failure": quotex_client.last_failure_kind(),
        "reconnects": app_state.get("reconnects", 0),
        "feed_stale_seconds": (
            int(app_state["feed_manager"].seconds_since_last_tick()) if app_state.get("feed_manager") else None
        ),
        "session_persistence": railway_control.status(),
    }


class SessionUpdate(BaseModel):
    admin_token: str
    session_token: str
    session_cookies: str = ""


@app.post("/api/session")
async def update_session(payload: SessionUpdate):
    if not config.ADMIN_TOKEN or payload.admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin passcode")

    session_token = payload.session_token.strip()
    if not session_token:
        raise HTTPException(status_code=400, detail="session_token is required")

    await quotex_client.set_manual_session(session_token, payload.session_cookies.strip())

    asyncio.create_task(_restart_feed())
    return {"ok": True, "message": "Session updated — reconnecting in the background"}


@app.get("/api/pairs")
async def pairs():
    return {"otc": list(config.OTC_PAIRS.keys()), "forex": list(config.FOREX_PAIRS.keys())}


@app.get("/api/live")
async def live_signals(tier: str | None = None):
    """Public, no-auth snapshot of the current CALL/PUT call for every
    pair — the latest signal each has fired, whether still pending or
    already graded. Meant for external scripts/bots to poll instead of
    holding a WebSocket connection open.

    `tier=confirmed` returns only signals that passed the quality gates.
    Everything else is `tier=noise`: the ALWAYS_SIGNAL filler that exists
    so each pair always shows *something*, and which is no better than a
    coin flip. `age_seconds` is how long ago the call was made — a pair
    whose stream has stalled will keep returning its last signal, and
    without the age there's no way to tell that from a fresh one.
    """
    now = int(time.time())
    rows = await db.latest_signals()
    return [
        {
            "pair": r["pair"],
            "direction": r["direction"],
            "confidence": r["confidence"],
            "tier": r["tier"],
            "result": r["result"],
            "entry_ts": r["entry_ts"],
            "target_close_ts": r["target_close_ts"],
            "entry_price": r["entry_price"],
            "close_price": r["close_price"],
            "source": r["source"],
            "created_at": r["created_at"],
            "age_seconds": now - r["created_at"],
            "stale": now - r["created_at"] > 3 * config.CANDLE_PERIOD_SECONDS,
        }
        for r in rows
        if tier is None or r["tier"] == tier
    ]


@app.get("/api/patterns")
async def patterns(pair: str | None = None):
    return await db.pattern_performance(pair)


@app.get("/api/backtest")
async def backtest_endpoint(pair: str | None = None, limit: int = 5000):
    """Scores every strategy independently against the stored candles.

    Unlike /api/patterns — which only sees sources that made it onto a
    fired signal — this asks each source in isolation whether it was
    right, on identical data, so the numbers are comparable and the
    weight each has earned is visible. Read-only.
    """
    limit = max(100, min(limit, 20000))
    if pair:
        return {"pairs": [await backtest.backtest_pair(pair, limit)], "across_pairs": []}
    return await backtest.backtest_all(limit)


@app.get("/api/candles")
async def candles(pair: str, limit: int = 200):
    return await db.recent_candles(pair, limit)


@app.get("/api/history")
async def history(pair: str | None = None, limit: int = 200):
    return await db.history(pair, limit)


@app.get("/api/winrate")
async def winrate(pair: str | None = None):
    return await db.win_rate(pair)


# ---------------------------------------------------------------------------
# Open public signal API — anyone can fetch the current CALL/PUT calls.
# Per the user requirement: open URLs, no auth, suitable for external
# bots/scripts to consume directly.
# ---------------------------------------------------------------------------

def _format_signal_row(r: dict) -> dict:
    now = int(time.time())
    return {
        "pair": r["pair"],
        "direction": r["direction"],
        "confidence": r["confidence"],
        "tier": r["tier"],
        "result": r["result"],
        "entry_ts": r["entry_ts"],
        "target_close_ts": r["target_close_ts"],
        "entry_price": r["entry_price"],
        "close_price": r["close_price"],
        "source": r["source"],
        "sources": [s for s in (r["source"] or "").split(",") if s],
        "created_at": r["created_at"],
        "age_seconds": now - r["created_at"],
        "stale": now - r["created_at"] > 3 * config.CANDLE_PERIOD_SECONDS,
        "expires_in_seconds": max(0, r["target_close_ts"] - now),
    }


@app.get("/api/signals")
async def signals_all(tier: str | None = None):
    """All pairs' latest CALL/PUT signal, open to anyone.

    Query params:
      tier=confirmed  — only quality-gated signals
      tier=noise      — only the ALWAYS_SIGNAL filler (no quality gate)

    Each row includes:
      pair, direction (CALL|PUT), confidence (0..1 or null),
      tier (confirmed|noise), result (PENDING|WIN|LOSS|DRAW),
      entry_ts, target_close_ts, entry_price, close_price,
      source (comma-joined), sources (list), created_at, age_seconds,
      stale (bool), expires_in_seconds (>=0).

    This is the same payload as /api/live but with extra convenience
    fields (sources list, expires_in_seconds) for direct bot/script use.
    """
    rows = await db.latest_signals()
    out = [_format_signal_row(r) for r in rows if tier is None or r["tier"] == tier]
    return {
        "server_time": int(time.time()),
        "candle_period_seconds": config.CANDLE_PERIOD_SECONDS,
        "count": len(out),
        "signals": out,
    }


@app.get("/api/signals/pair")
async def signals_for_pair(pair: str):
    """Latest signal for one specific pair, open to anyone.

    Use as: GET /api/signals/pair?pair=EUR/USD
    (We use a query param rather than a path param because pair names
    like "EUR/USD" contain a slash, which FastAPI path params would
    interpret as a URL separator.)

    Returns 404 if the pair is unknown or has never produced a signal.
    """
    if pair not in config.ALL_PAIRS:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    rows = await db.latest_signals()
    for r in rows:
        if r["pair"] == pair:
            return _format_signal_row(r)
    raise HTTPException(status_code=404, detail=f"no signal yet for pair: {pair}")


@app.get("/api/signals/history")
async def signals_history(pair: str, limit: int = 100):
    """Historical signals for one pair (newest first). Open to anyone.

    Use as: GET /api/signals/history?pair=EUR/USD&limit=100
    """
    if pair not in config.ALL_PAIRS:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    limit = max(1, min(limit, 1000))
    rows = await db.history(pair, limit)
    return {
        "pair": pair,
        "count": len(rows),
        "history": [_format_signal_row(r) for r in rows],
    }


@app.get("/api/strategies")
async def strategies_registry():
    """Open registry of every candlestick pattern the engine knows about.
    Useful for documentation and for downstream consumers that want to
    understand which sources are firing on their signals."""
    return {
        "patterns": patterns_module.PATTERN_REGISTRY,
        "indicator_sources": [
            {"name": "rsi_oversold",        "family": "indicator", "description": "RSI < 35 → CALL (oversold bounce)"},
            {"name": "rsi_overbought",      "family": "indicator", "description": "RSI > 65 → PUT (overbought reversal)"},
            {"name": "ema_trend_up",        "family": "indicator", "description": "Close > EMA50 + 0.5 ATR → CALL"},
            {"name": "ema_trend_down",      "family": "indicator", "description": "Close < EMA50 - 0.5 ATR → PUT"},
            {"name": "bb_lower_bounce",     "family": "indicator", "description": "%B ≤ 0.05 → CALL"},
            {"name": "bb_upper_rejection",  "family": "indicator", "description": "%B ≥ 0.95 → PUT"},
            {"name": "bb_squeeze_break_up",  "family": "indicator", "description": "Tight BB + close above upper → CALL"},
            {"name": "bb_squeeze_break_down","family": "indicator", "description": "Tight BB + close below lower → PUT"},
            {"name": "sma_crossover",       "family": "indicator", "description": "Fresh SMA(8)/SMA(21) cross"},
            {"name": "near_resistance",     "family": "indicator", "description": "Close within 0.3 ATR of fractal resistance → PUT"},
            {"name": "near_support",        "family": "indicator", "description": "Close within 0.3 ATR of fractal support → CALL"},
        ],
        "reaction_sources": [
            {"name": "fractal_rejection_top",    "family": "reaction", "description": "Upper wick rejected at fractal swing high → PUT"},
            {"name": "fractal_rejection_bottom", "family": "reaction", "description": "Lower wick rejected at fractal swing low → CALL"},
            {"name": "ema_rejection_top",        "family": "reaction", "description": "Upper wick rejected at EMA50 → PUT"},
            {"name": "ema_rejection_bottom",     "family": "reaction", "description": "Lower wick rejected at EMA50 → CALL"},
            {"name": "bb_rejection_top",         "family": "reaction", "description": "Wick above BB upper, close inside → PUT"},
            {"name": "bb_rejection_bottom",      "family": "reaction", "description": "Wick below BB lower, close inside → CALL"},
            {"name": "round_number_rejection_top",    "family": "reaction", "description": "Upper wick rejected at round number → PUT"},
            {"name": "round_number_rejection_bottom", "family": "reaction", "description": "Lower wick rejected at round number → CALL"},
        ],
        "microstructure_sources": [
            {"name": "microstructure", "family": "microstructure", "description": "Blended candle color/body/wick/trend/streak score"},
        ],
        "mined_sources": [
            {"name": "mined_*", "family": "mined", "description": "Self-learned 2-candle sequences promoted via FDR correction"},
        ],
        "fallback_sources": [
            {"name": "fallback_color", "family": "fallback", "description": "ALWAYS_SIGNAL filler: previous candle's color (noise tier)"},
        ],
    }


@app.get("/api/docs/openapi.json")
async def openapi_json():
    """Convenience redirect to FastAPI's built-in OpenAPI spec."""
    return app.openapi()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # client doesn't need to send anything; keeps connection open
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
