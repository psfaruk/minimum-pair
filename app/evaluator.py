import asyncio
import json
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
    """Attempts to grade a single pending signal. Returns True if the
    signal transitioned PENDING -> graded THIS call.

    The transition guard matters for accuracy: stats (pattern_stats,
    signal_stats) are bumped only when this call is the one that actually
    graded the row. A re-run that finds the row already graded must not
    count the same trade twice — one duplicated tally silently corrupts
    every win rate it flows into."""
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
            return await db.grade_signal(signal["id"], None, "DRAW")
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

    transitioned = await db.grade_signal(signal["id"], close_price, result)
    if not transitioned:
        # Another grader got here first — never count this trade twice.
        return False

    if result != "DRAW":
        outcome_direction = (
            signal["direction"]
            if result == "WIN"
            else ("PUT" if signal["direction"] == "CALL" else "CALL")
        )

        # Grade EVERY vote this candle produced, each on its OWN
        # direction — not just the sources that ended up on the fired
        # signal. The old rule graded only the majority side, so a source
        # that kept voting the wrong direction but losing the weight
        # contest was invisible to the learning loop: no losses ever
        # accumulated, its weight never fell, and the engine kept acting
        # on its opinion. all_sources (persisted by feed.py from the
        # decision's full vote list) fixes that; legacy rows without it
        # fall back to the old majority-side accounting.
        votes: list[tuple[str, str]] = []
        if signal.get("all_sources"):
            try:
                parsed = json.loads(signal["all_sources"])
                votes = [(v["source"], v["direction"]) for v in parsed if v.get("source")]
            except (ValueError, TypeError, KeyError):
                votes = []
        if not votes:
            votes = [
                (s, signal["direction"])
                for s in (signal["source"] or "").split(",")
                if s
            ]

        seen: set[str] = set()
        for source, direction in votes:
            if source in seen:
                continue
            seen.add(source)
            await db.bump_pattern_stat(source, pair, direction == outcome_direction)

    # 2026-09 (confluence v3) — the signal's OWN strategy (its signature:
    # direction|regime|agreeing-families) learns from this outcome. This
    # is the record the decision engine gates future signals on: a
    # confluence that keeps losing on this pair falls below
    # MIN_CONFIDENCE and goes silent, while proven confluences keep
    # firing. Draws are skipped (stake refunded, no evidence).
    if signal.get("signature"):
        await db.bump_signal_stat(signal["signature"], pair, result == "WIN")

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
