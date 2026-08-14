import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, db, quotex_client
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    app_state["bootstrap_task"] = asyncio.create_task(_bootstrap_feed())
    asyncio.create_task(run_evaluator(_on_graded))
    yield


app = FastAPI(lifespan=lifespan)


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

    config.QUOTEX_SESSION_TOKEN = session_token
    config.QUOTEX_SESSION_COOKIES = payload.session_cookies.strip()
    quotex_client.reset_session_failures()

    asyncio.create_task(_restart_feed())
    return {"ok": True, "message": "Session updated — reconnecting in the background"}


@app.get("/api/pairs")
async def pairs():
    return {"otc": list(config.OTC_PAIRS.keys()), "forex": list(config.FOREX_PAIRS.keys())}


@app.get("/api/patterns")
async def patterns(pair: str | None = None):
    return await db.pattern_performance(pair)


@app.get("/api/candles")
async def candles(pair: str, limit: int = 200):
    return await db.recent_candles(pair, limit)


@app.get("/api/history")
async def history(pair: str | None = None, limit: int = 200):
    return await db.history(pair, limit)


@app.get("/api/winrate")
async def winrate(pair: str | None = None):
    return await db.win_rate(pair)


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
