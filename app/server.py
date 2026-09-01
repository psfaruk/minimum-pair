import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import backtest, config, db, patterns as patterns_module, quotex_client
from app.evaluator import run_evaluator
from app.feed import FeedManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Bumped on every production-visible change so a redeploy can be verified
# from outside: /api/status exposes it, and Railway has no other way to
# tell which commit a running instance was built from.
CODE_VERSION = "2026.09.02-deploy-diagnostics"

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


# How long the bootstrap loop waits between connection attempts when the
# last one failed — including "no token configured yet". Governs the
# cadence at which a freshly pasted token is picked up after /api/session,
# and how often the app re-checks while waiting for one.
RETRY_BACKOFF_SECONDS = 20


async def _bootstrap_feed() -> None:
    """Brings the Quotex feed online.

    Token-only: with no session token configured, get_client() raises
    NoSessionTokenError immediately, which this loop treats the same as
    any other failure — wait, then try again. Pasting a token via
    /api/session cancels this task and starts a fresh one, which then
    succeeds.
    """
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
        except quotex_client.NoSessionTokenError as e:
            app_state["quotex_connected"] = False
            app_state["error"] = str(e)
            logger.info("No session token yet — waiting %ds before re-checking (attempt %d)", RETRY_BACKOFF_SECONDS, attempt)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        except Exception as e:
            app_state["quotex_connected"] = False
            app_state["error"] = str(e)
            logger.exception("Failed to start Quotex feed (attempt %d), retrying in %ds", attempt, RETRY_BACKOFF_SECONDS)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)


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


# Cap on how far the watchdog's rebuild backoff can stretch, in multiples
# of CONNECTION_WATCHDOG_SECONDS. At the default 60s that's a 10-minute
# ceiling between attempts once the connection has stayed dead for a
# while — still recovers well inside a market session, but stops
# hammering the broker every single check interval forever.
MAX_WATCHDOG_BACKOFF_MULTIPLE = 10


async def _connection_watchdog() -> None:
    """Rebuilds the connection when the feed goes silent.

    A session token that expires mid-run doesn't announce itself: no
    exception, no disconnect callback, the websocket just stops
    delivering ticks. Nothing else in the app would ever notice, so the
    app would sit there "connected" and signal-less until someone
    restarted it. OTC pairs trade around the clock, so silence across
    *every* pair means the connection is gone, not that the market is
    closed. The restart reuses the pasted token — there is no password
    path to fall back to.

    Rebuilding means a full teardown and a fresh Quotex login with that
    same static token. Retrying that every single
    CONNECTION_WATCHDOG_SECONDS with no backoff — which is what this used
    to do — means dozens of full re-authentications per hour whenever the
    feed doesn't recover on the first try, which is exactly the kind of
    repeated-login pattern that gets a broker session invalidated well
    before its normal lifetime (observed: tokens that should last ~24h
    dying within 2-3h). So each consecutive rebuild that doesn't bring
    ticks back waits longer before the next attempt.
    """
    consecutive_rebuilds = 0
    # Consecutive checks where the client reported itself disconnected
    # while the feed wasn't stale yet. check_connect() is honest now, so
    # a brief network blip shows up as a momentary "not connected" — but
    # pyquotex's auto-reconnect (+ its re-authorization) heals that
    # within seconds, long before the next check. Tearing the whole feed
    # down on the FIRST disconnected check converted every blip into a
    # multi-minute outage; a disconnect must persist across two checks
    # before a full rebuild is worth its cost.
    disconnected_checks = 0
    while True:
        await asyncio.sleep(config.CONNECTION_WATCHDOG_SECONDS)
        manager = app_state.get("feed_manager")
        if manager is None:
            consecutive_rebuilds = 0
            disconnected_checks = 0
            app_state["consecutive_rebuild_failures"] = 0
            continue  # the bootstrap loop already owns the reconnect

        stale_for = manager.seconds_since_last_tick()
        connected = await quotex_client.is_connected()
        if connected and stale_for < config.STALE_FEED_SECONDS:
            consecutive_rebuilds = 0
            disconnected_checks = 0
            app_state["consecutive_rebuild_failures"] = 0
            continue

        if not connected and stale_for < config.STALE_FEED_SECONDS:
            # Momentary disconnect with the feed itself still fresh — the
            # auto-reconnector owns this, not us. Wait for it.
            disconnected_checks += 1
            if disconnected_checks < 2:
                logger.info(
                    "Client briefly disconnected (no ticks for %.0fs) — "
                    "giving the auto-reconnect one more cycle before a rebuild",
                    stale_for,
                )
                continue
        disconnected_checks = 0

        consecutive_rebuilds += 1
        app_state["consecutive_rebuild_failures"] = consecutive_rebuilds
        extra_wait = min(consecutive_rebuilds - 1, MAX_WATCHDOG_BACKOFF_MULTIPLE) * config.CONNECTION_WATCHDOG_SECONDS
        if extra_wait:
            logger.warning(
                "Connection still dead after %d consecutive rebuild attempts — "
                "backing off an extra %ds before trying again",
                consecutive_rebuilds,
                extra_wait,
            )
            await asyncio.sleep(extra_wait)

        logger.warning(
            "Connection looks dead (authenticated=%s, no ticks for %.0fs) — rebuilding it (attempt %d)",
            connected,
            stale_for,
            consecutive_rebuilds,
        )
        app_state["reconnects"] = app_state.get("reconnects", 0) + 1
        await _restart_feed()


async def _prune_loop() -> None:
    """Keeps the candles/signals tables from growing forever. With the
    per-candle guarantee, 16 pairs write a row per pair per 60s candle
    close — tens of thousands of rows a day, unbounded, on a billed
    Railway volume. pattern_stats (what weights.py actually learns
    from) is a separate aggregate table this never touches."""
    while True:
        # Sleep first: running immediately at boot would pile a DELETE
        # sweep on top of the bootstrap burst (16 pairs' history backfill
        # + initial connects), the busiest and most contention-prone
        # moment the DB ever sees.
        await asyncio.sleep(config.PRUNE_INTERVAL_SECONDS)
        try:
            deleted_candles, deleted_signals = await db.prune_old_data(
                config.CANDLE_RETENTION_DAYS, config.SIGNAL_RETENTION_DAYS
            )
            if deleted_candles or deleted_signals:
                logger.info(
                    "Pruned %d candle rows older than %dd and %d graded signal rows older than %dd",
                    deleted_candles, config.CANDLE_RETENTION_DAYS,
                    deleted_signals, config.SIGNAL_RETENTION_DAYS,
                )
        except Exception:
            logger.exception("Prune loop error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    # Must run before the first connect: it restores any session token
    # captured earlier (Railway restarts would otherwise lose it).
    await quotex_client.load_persisted_state()
    app_state["bootstrap_task"] = asyncio.create_task(_bootstrap_feed())
    asyncio.create_task(run_evaluator(_on_graded))
    asyncio.create_task(_connection_watchdog())
    asyncio.create_task(_prune_loop())
    yield


app = FastAPI(lifespan=lifespan)

# Open public API: anyone can fetch the current CALL/PUT signal for every
# pair via plain HTTP GET. Per the user requirement: open URLs, no auth,
# suitable for external bots/scripts to consume directly. CORS is open
# (`Access-Control-Allow-Origin: *`) so browser-side scripts and bots on
# other domains can hit these directly.
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
        "code_version": CODE_VERSION,
        "quotex_connected": app_state["quotex_connected"],
        "error": app_state["error"],
        "pairs": app_state["pairs"],
        "active_pairs": list(app_state.get("feed_manager").pairs.keys()) if app_state.get("feed_manager") else [],
        "min_confidence": config.MIN_CONFIDENCE,
        "account_mode": config.QUOTEX_ACCOUNT_MODE,
        "auth_mode": quotex_client.auth_mode(),
        "reconnects": app_state.get("reconnects", 0),
        "consecutive_rebuild_failures": app_state.get("consecutive_rebuild_failures", 0),
        # The most informative description of the last connect attempt's
        # outcome. Without it, a deployed instance showing no data is
        # indistinguishable from one that is simply still warming up.
        "last_connect_detail": quotex_client.last_connect_detail(),
        "total_ticks": (
            app_state["feed_manager"].total_ticks if app_state.get("feed_manager") else 0
        ),
        # Set by the boot migration once the one-time repair + dedupe
        # recount of pattern_stats has run — proves the new code booted
        # and repaired this instance's database.
        "migration_deduped": bool(await db.get_state("pattern_stats_deduped_v2")),
        # Per-pair regime read at each pair's latest signal: "trend",
        # "range" or "neutral". Tells consumers what market condition the
        # engine believed it was trading into when it last fired.
        "regimes": {
            name: state.last_regime
            for name, state in app_state["feed_manager"].pairs.items()
        } if app_state.get("feed_manager") else {},
        "feed_stale_seconds": (
            int(app_state["feed_manager"].seconds_since_last_tick()) if app_state.get("feed_manager") else None
        ),
    }


# --- Deployment self-diagnostics -------------------------------------------
#
# "Deploy করার পর ডেটা আসছে না" has exactly five distinct causes and the UI
# used to show the same "সংযোগ হচ্ছে…" for all of them. /api/diagnose runs
# the real pipeline step by step INSIDE whatever environment this instance
# lives in (Railway container, VPS, laptop) and reports which step breaks:
#
#   1. token      — env var / persisted token present?
#   2. connect    — websocket handshake + SSID authorization accepted?
#   3. assets     — instrument list received?
#   4. stream     — price frames pushed after instruments/update?
#   5. history    — candle history endpoint answering?
#
# Each step short-circuits the next: if auth is rejected there is no point
# probing the stream. The verdict includes an actionable fix hint in
# Bengali, because the person staring at empty charts reads Bengali —
# that is the whole point of this endpoint.

DIAGNOSE_STREAM_PROBE_SECONDS = 12
DIAGNOSE_HISTORY_TIMEOUT_SECONDS = 10


def _verdict(ok: bool, problem_bn: str, fix_bn: str, step_failed: str | None = None) -> dict:
    v = {
        "ok": ok,
        "problem_bn": problem_bn if not ok else None,
        "fix_bn": fix_bn if not ok else None,
    }
    if step_failed:
        v["failed_step"] = step_failed
    return v


async def _diagnose_stream_probe(client, assets: list[str]) -> dict:
    """Subscribes to up to two assets and counts raw price frames pushed
    over the wire — the closest possible probe of what _run_pair consumes
    without touching the live feed's own client."""
    target = assets[:2]
    counts = {a: 0 for a in target}
    before = {a: len(client.api.realtime_price.get(a, [])) for a in target}

    for a in target:
        try:
            await client.api.subscribe_realtime_candle(a, config.CANDLE_PERIOD_SECONDS)
            await client.api.chart_notification(a)
            await client.api.follow_candle(a)
        except Exception as e:  # send failure — socket already dead
            return {"assets": target, "error": f"subscribe send failed: {type(e).__name__}: {e}"}

    await asyncio.sleep(DIAGNOSE_STREAM_PROBE_SECONDS)

    details = {}
    for a in target:
        now = len(client.api.realtime_price.get(a, []))
        counts[a] = max(0, now - before[a])
        details[a] = counts[a]
    return {"assets": target, "ticks": details, "total": sum(counts.values())}


@app.get("/api/diagnose")
async def diagnose():
    """Runs the full broker pipeline inside this deployment and reports
    exactly which step fails. Read-only: uses a throwaway client on its
    own websocket so the live feed is never disturbed."""
    steps: dict = {}
    started = time.monotonic()

    # Step 1: token presence (env var first, then DB-persisted paste).
    # os.environ is consulted directly so a runtime-pasted token isn't
    # mislabeled as an env-var one.
    token_env = bool(os.environ.get("QUOTEX_SESSION_TOKEN"))
    token_pasted = bool(config.QUOTEX_SESSION_TOKEN)
    token_db = False
    if not token_pasted:
        try:
            state = await db.get_state("quotex_session_token")
            token_db = bool(state.get("quotex_session_token"))
        except Exception:
            token_db = False
    steps["token"] = {
        "source": ("env" if token_env else "pasted") if token_pasted else ("database" if token_db else None),
        "present": token_pasted or token_db,
    }
    if not token_pasted and not token_db:
        steps["verdict"] = _verdict(
            False,
            "সেশন টোকেন দেওয়া হয়নি — তাই কোনো ডেটা আসবে না।",
            "Railway-এর Variables-এ QUOTEX_SESSION_TOKEN সেট করুন, অথবা অ্যাপের Settings ট্যাবে সেশন টোকেন পেস্ট করুন।",
            "token",
        )
        return {**steps, "elapsed_seconds": round(time.monotonic() - started, 1)}

    # Step 2-5: live pipeline probe on a throwaway client.
    from pyquotex.stable_api import Quotex

    client = Quotex(
        email="",
        password="",
        lang=config.QUOTEX_LANG,
        root_path=str(config.SESSION_ROOT),
    )
    client.set_session(
        user_agent=config.QUOTEX_USER_AGENT or quotex_client.DEFAULT_USER_AGENT,
        cookies=config.QUOTEX_SESSION_COOKIES or None,
        ssid=config.QUOTEX_SESSION_TOKEN,
    )

    t0 = time.monotonic()
    try:
        ok, reason = await client.connect()
    except Exception as e:
        ok, reason = False, f"{type(e).__name__}: {e}"
    steps["connect"] = {
        "ok": ok,
        "reason": reason,
        "seconds": round(time.monotonic() - t0, 1),
    }

    if not ok:
        # Give the socket a short grace to authorize (mirrors get_client's
        # logic, compressed — diagnostics must stay fast).
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if await client.check_connect():
                ok = True
                break
        steps["connect"]["ok"] = ok
        if not ok:
            raw = ""
            try:
                raw = str(client.api.state.websocket_error_reason or "")
            except Exception:
                pass
            detail = f"{reason} ({raw})" if raw and raw not in reason else reason
            steps["connect"]["reason"] = detail
            steps["verdict"] = _verdict(
                False,
                "Quotex সার্ভার সংযোগ/লগইন রিজেক্ট করেছে — এটাই ডেটা না আসার কারণ।",
                "১) টোকেনটি কি এখনও বৈধ? নতুন করে qxbroker.com-এ লগইন করে সদ্য কপি করা SSID টোকেন Settings-এ পেস্ট করুন। "
                "২) Railway-এর region পরিবর্তন করে দেখুন (broker কিছু datacenter IP/রিজিওন ব্লক করে)। "
                "৩) একই টোকেন একাধিক জায়গায় (লোকাল + সার্ভার) একসাথে চালাবেন না।",
                "connect",
            )
            try:
                await client.close()
            except Exception:
                pass
            return {**steps, "elapsed_seconds": round(time.monotonic() - started, 1)}

    steps["authorized"] = {"ok": True}

    # Step 3: instrument list.
    try:
        assets = await client.get_all_assets()
    except Exception as e:
        assets = []
        steps["assets"] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
    steps["assets"] = {"count": len(assets)}
    if not assets:
        steps["verdict"] = _verdict(
            False,
            "লগইন সফল, কিন্তু ব্রোকারের ইনস্ট্রুমেন্ট তালিকা আসেনি।",
            "৩০ সেকেন্ড পর আবার ডায়াগনোসিস চালান; বারবার একই হলে Railway region পরিবর্তন করুন।",
            "assets",
        )
        try:
            await client.close()
        except Exception:
            pass
        return {**steps, "elapsed_seconds": round(time.monotonic() - started, 1)}

    # Step 4: live price-stream probe on a configured pair the broker lists.
    probe_candidates = []
    for candidates in config.ALL_PAIRS.values():
        for code in candidates:
            if code in assets and code not in probe_candidates:
                probe_candidates.append(code)
    probe = await _diagnose_stream_probe(client, probe_candidates or list(assets)[:2])
    steps["stream"] = probe
    stream_total = probe.get("total", 0)
    if stream_total <= 0:
        steps["verdict"] = _verdict(
            False,
            "লগইন ও সাবস্ক্রিপশন ঠিক, কিন্তু ব্রোকার কোনো প্রাইস টিক পাঠাচ্ছে না (টেস্ট করা পেয়ারগুলো সম্ভবত বন্ধ বা এই সার্ভার IP-তে স্ট্রিম ব্লকড)।",
            "১) Railway-এর ভিন্ন region-এ redeploy করুন। ২) qxbroker.com ওয়েবসাইটে ওই পেয়ারের চার্ট খুলে দেখুন মার্কেট খোলা আছে কি না। "
            "৩) সব পেয়ার বন্ধ থাকলে (মার্কেট ক্লোজ) ডেটা না আসা স্বাভাবিক — মার্কেট খুললেই আসবে।",
            "stream",
        )
        try:
            await client.close()
        except Exception:
            pass
        return {**steps, "elapsed_seconds": round(time.monotonic() - started, 1)}

    # Step 5: candle-history endpoint answers?
    probe_asset = (probe.get("assets") or [None])[0]
    history_ok, history_count = False, 0
    if probe_asset:
        try:
            rows = await client.get_candles(
                probe_asset,
                int(time.time()),
                config.CANDLE_PERIOD_SECONDS * 40,
                config.CANDLE_PERIOD_SECONDS,
                timeout=DIAGNOSE_HISTORY_TIMEOUT_SECONDS,
            )
            history_count = len(rows or [])
            history_ok = history_count > 0
        except Exception:
            history_ok = False
    steps["history"] = {"ok": history_ok, "candles": history_count, "asset": probe_asset}
    if not history_ok:
        steps["verdict"] = _verdict(
            False,
            "লাইভ টিক আসছে, কিন্তু ক্যান্ডেল-হিস্ট্রি এন্ডপয়েন্ট সাড়া দিচ্ছে না — চার্ট ও সিগন্যাল শুরু হতে দেরি হবে।",
            "এটি সাধারণত কিছুক্ষণ পরেই ঠিক হয়ে যায় (ওয়ার্মআপ)। ৫ মিনিট পর আবার ডায়াগনোসিস চালান।",
            "history",
        )
    else:
        steps["verdict"] = _verdict(
            True,
            None,
            None,
        )
        steps["verdict"]["all_ok_bn"] = (
            "সব ধাপ পাস — টোকেন, লগইন, স্ট্রিম, হিস্ট্রি সব ঠিক আছে। ডেটা এখন আসা উচিত।"
        )

    try:
        await client.close()
    except Exception:
        pass
    return {**steps, "elapsed_seconds": round(time.monotonic() - started, 1)}


class SessionUpdate(BaseModel):
    # Token-only auth surface: a single `session_token` (the SSID string
    # pasted from a logged-in browser session) is the entire credential.
    # The optional cookies field carries cf_clearance / laravel_session
    # for browsers that need them — together they let the websocket
    # authenticate without ever touching the Cloudflare-guarded login
    # page. No admin passcode, no API keys, no other auth fields.
    session_token: str
    session_cookies: str = ""


@app.post("/api/session")
async def update_session(payload: SessionUpdate):
    """Paste a fresh Quotex session token. This is the ONLY way the
    frontend authenticates against the broker — no admin passcode, no
    API keys, nothing else is asked of the user.

    Stored like a captured one so a restart keeps using it. Pasting a
    fresh token also clears any prior one-shot password-login failure
    flag, so the next boot is back to a clean slate."""
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

    Every real finalized candle fires a signal — the per-candle
    guarantee. `tier=confirmed` returns only signals that passed the
    quality gates. Everything else is `tier=fallback`: the best-effort
    filler that exists so each pair always shows *something*, and which
    carries no measured confidence. (Rows fired before 2026-08-30 may
    carry the old tier name `noise` for the same concept.) `age_seconds`
    is how long ago the call was made — a pair whose stream has stalled
    will keep returning its last signal, and without the age there's no
    way to tell that from a fresh one.
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
async def history(
    pair: str | None = None,
    limit: int = 200,
    direction: str | None = None,
    tier: str | None = None,
    result: str | None = None,
    offset: int = 0,
):
    """Historical signals, newest first, with optional filters.

    2026-09: `direction` (CALL|PUT), `tier` (confirmed|fallback),
    `result` (WIN|LOSS|DRAW|PENDING) and `offset` (pagination) let the UI
    and scripts slice the history however they want.
    """
    limit = max(1, min(limit, 1000))
    return await db.history(pair, limit, direction, tier, result, offset)


@app.get("/api/winrate")
async def winrate(pair: str | None = None, days: int = 0):
    """Per-pair win rates with CALL/PUT and confirmed/fallback splits.

    2026-09: each row now carries `call` and `put` objects (wins, losses,
    pending, win_rate) plus `confirmed` and `fallback` splits, so the UI
    can show exactly which direction and which tier is paying on which
    pair ("Call ও put কোনো সিগন্যাল গুলো কেমন win রেট দিচ্ছে"). The
    original flat fields (wins/losses/draws/pending/win_rate) are
    unchanged for existing consumers.

    `days` limits the window (0 = all history, 1 = last 24h, 7 = week).
    """
    days = max(0, min(days, 90))
    return await db.win_rate_ext(pair, days)


@app.get("/api/winrate/summary")
async def winrate_summary(days: int = 0):
    """Global win-rate summary over the window: totals, CALL/PUT split,
    confirmed/fallback split. `days` = 0 means all history."""
    days = max(0, min(days, 90))
    return await db.summary(days)


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

    Per the per-candle guarantee, every real finalized candle fires a
    signal — either `confirmed` (passed the quality gates: regime-
    weighted confluence, structural weight, veto checks, and measured
    confidence once there's enough graded history) or `fallback` (the
    best-effort filler that exists so each pair always shows
    *something*, no better than the engine's best guess).

    Query params:
      tier=confirmed  — only quality-gated signals
      tier=fallback   — only the best-effort filler (no quality gate)

    Each row includes:
      pair, direction (CALL|PUT), confidence (0..1 or null),
      tier (confirmed|fallback), result (PENDING|WIN|LOSS|DRAW),
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
async def signals_history(
    pair: str,
    limit: int = 100,
    direction: str | None = None,
    tier: str | None = None,
    result: str | None = None,
    offset: int = 0,
):
    """Historical signals for one pair (newest first). Open to anyone.

    Use as: GET /api/signals/history?pair=EUR/USD&limit=100
    Optional filters: direction=CALL|PUT, tier=confirmed|fallback,
    result=WIN|LOSS|DRAW|PENDING, offset for pagination.
    """
    if pair not in config.ALL_PAIRS:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    limit = max(1, min(limit, 1000))
    rows = await db.history(pair, limit, direction, tier, result, offset)
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
        "regime_sources": [
            {"name": "anchor_fade",      "family": "regime", "description": "Price stretched >= 1 ATR from its 60-candle mean → fade (range/neutral regime)"},
            {"name": "anchor_follow",    "family": "regime", "description": "Displacement >= 1 ATR continues (confirmed trend regime)"},
            {"name": "streak_exhaustion", "family": "regime", "description": "3+ same-colour candles in a row → fade the streak"},
            {"name": "htf_trend",         "family": "regime", "description": "5-minute higher-timeframe leg alignment"},
        ],
        "mined_sources": [
            {"name": "mined_*", "family": "mined", "description": "Self-learned 2-candle sequences promoted via FDR correction"},
        ],
        "fallback_sources": [
            {"name": "fallback_color", "family": "fallback", "description": "Per-candle-guarantee last resort: previous candle's color (fallback tier only, used when nothing else fired)"},
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
