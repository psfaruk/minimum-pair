import asyncio
import logging
import time
from typing import Awaitable, Callable

from app import db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# If the target candle still hasn't appeared this long after it should
# have closed, it never will — the stream died, or the app restarted
# through the gap. Those signals used to stay PENDING forever and get
# re-queried every POLL_INTERVAL_SECONDS for the life of the database.
ABANDON_AFTER_SECONDS = 600

# A synthetic candle at grading time might just be a backfill race, not a
# genuinely quiet minute: feed.py fires a background fetch of the
# broker's real OHLC the moment a candle finalizes synthetic, but every
# pair's fetch is serialized through one lock (see feed.py's
# _BACKFILL_LOCK — pyquotex correlates get_candles() responses through
# shared connection state, so concurrent fetches interleave and all time
# out). Measured live: a full burst of 16 pairs going synthetic in the
# same minute took up to ~100s to fully drain through that single queue.
# Grading DRAW immediately would beat the backfill on nearly every
# occurrence and throw away real outcomes for no reason, so this grace
# window has to comfortably clear that worst case.
SYNTHETIC_GRACE_SECONDS = 150


async def _grade_one(signal: dict, on_graded: Callable[[dict], Awaitable[None]]) -> bool:
    """Attempts to grade a single pending signal. Returns True if graded."""
    pair = signal["pair"]
    candle = await db.get_candle(pair, signal["entry_ts"])
    if not candle:
        overdue = int(time.time()) - signal["target_close_ts"]
        if overdue > ABANDON_AFTER_SECONDS:
            logger.info(
                "No candle ever arrived for %s at %s (%ds overdue) — recording signal %s as a draw",
                pair,
                signal["entry_ts"],
                overdue,
                signal["id"],
            )
            await db.grade_signal(signal["id"], None, "DRAW")
            return True
        return False  # target candle not persisted yet, retry later

    entry_price = signal["entry_price"]
    close_price = candle["close"]
    if entry_price is None:
        entry_price = candle["open"]

    if candle.get("synthetic"):
        # The feed invented this candle because our own tick polling
        # observed nothing this minute. feed.py is already trying to
        # backfill the broker's real OHLC in the background — give that a
        # short grace window before conceding there's truly no outcome to
        # read off it, so a normal-speed backfill isn't beaten to the
        # punch by this loop's own 5s poll cadence.
        overdue = int(time.time()) - signal["target_close_ts"]
        if overdue < SYNTHETIC_GRACE_SECONDS:
            return False  # retry later — the backfill may still land
        result = "DRAW"
    elif close_price == entry_price:
        # Price finished exactly where it started. Brokers refund these,
        # so a loss is the wrong answer; counting them as losses is what
        # dragged the measured win rate ~15 points below reality on thin
        # OTC pairs, and it poisoned every source's learned statistics.
        result = "DRAW"
    elif signal["direction"] == "CALL":
        result = "WIN" if close_price > entry_price else "LOSS"
    else:
        result = "WIN" if close_price < entry_price else "LOSS"

    await db.grade_signal(signal["id"], close_price, result)

    if result != "DRAW":
        sources = [s for s in (signal["source"] or "").split(",") if s]
        for source in sources:
            await db.bump_pattern_stat(source, pair, result == "WIN")

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
