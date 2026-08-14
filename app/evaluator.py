import asyncio
import logging
import time
from typing import Awaitable, Callable

from app import db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def _grade_one(signal: dict, on_graded: Callable[[dict], Awaitable[None]]) -> bool:
    """Attempts to grade a single pending signal. Returns True if graded."""
    pair = signal["pair"]
    candle = await db.get_candle(pair, signal["entry_ts"])
    if not candle:
        return False  # target candle not persisted yet, retry later

    entry_price = signal["entry_price"]
    close_price = candle["close"]
    if entry_price is None:
        entry_price = candle["open"]

    if signal["direction"] == "CALL":
        won = close_price > entry_price
    else:
        won = close_price < entry_price
    result = "WIN" if won else "LOSS"

    await db.grade_signal(signal["id"], close_price, result)

    sources = [s for s in (signal["source"] or "").split(",") if s]
    for source in sources:
        await db.bump_pattern_stat(source, pair, won)

    graded = dict(signal)
    graded["close_price"] = close_price
    graded["result"] = result
    await on_graded(graded)
    return True


async def run_evaluator(on_graded: Callable[[dict], Awaitable[None]]) -> None:
    """Background loop: grades pending signals whose target candle has
    closed, updates per-source pattern stats, and notifies via
    `on_graded` (used to broadcast the result over the WS)."""
    while True:
        try:
            now = int(time.time())
            due = await db.pending_signals_due(now)
            for signal in due:
                try:
                    await _grade_one(signal, on_graded)
                except Exception:
                    logger.exception("Failed grading signal %s", signal.get("id"))
        except Exception:
            logger.exception("Evaluator loop error")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
