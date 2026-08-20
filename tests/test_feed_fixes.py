"""Verifies the feed-race fixes against a throwaway database.

Scenarios covered:
  1. A tick arriving after its minute already closed is folded into the
     finalized candle (no re-open, no duplicate signal, no fabric
     candle) and re-prices the PENDING signal that used the old close.
  2. A placeholder minute that later sees a real tick loses its
     synthetic flag and produces a real signal at its boundary.
  3. A second finalize of the same entry minute fires no second signal.
  4. A candle finalized after its entry minute closed fires no stale
     signal.
  5. The repair migration re-grades WIN/LOSS signals whose outcome
     candle is synthetic (race leftovers) to DRAW and rebuilds
     pattern_stats counting only the first signal per entry minute.

Run:  .venv/Scripts/python.exe tests/test_feed_fixes.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, feed  # noqa: E402


async def noop(*_args, **_kwargs):
    return None


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "test.db"

    await db.init_db()

    signals = []

    async def on_signal(_pair, payload):
        signals.append(payload)

    fm = feed.FeedManager(on_candle=noop, on_signal=on_signal)

    # --- scenario 1 + 2: normal boundary finalize, then a late-tick burst
    state = feed.PairState(display_name="TEST", asset_code="TEST")
    base = int(time.time())  # real clock so the stale guard sees real ages

    fm._apply_tick(state, base, 100.0)  # opens the minute's candle
    fm._apply_tick(state, base, 101.0)

    # Timer-style boundary finalize: the minute closed without a crossing
    # tick, so the next minute starts as a synthetic placeholder.
    finalized = state.current
    state.current = {
        "ts": base + 60, "open": 101.0, "high": 101.0, "low": 101.0,
        "close": 101.0, "synthetic": True,
    }
    await fm._finalize_candle(state, finalized)
    assert len(signals) == 1, f"expected 1 signal, got {len(signals)}"
    assert signals[0]["entry_ts"] == base + 60

    # Burst delivers ticks for the minute that just closed, late.
    late: dict[int, tuple[dict, float]] = {}
    assert fm._handle_tick(state, base, 101.5, late) is None
    assert fm._handle_tick(state, base, 101.8, late) is None
    await fm._flush_late_updates(state, late)

    # No duplicate signal was fired...
    assert len(signals) == 1, "late ticks must not fire a second signal"
    # ...the candle was folded in place, not re-created...
    row = await db.get_candle("TEST", base)
    assert row is not None
    assert row["synthetic"] == 0
    assert (row["low"], row["high"], row["close"]) == (100.0, 101.8, 101.8)
    # ...and the PENDING signal was re-priced to the corrected close.
    srow = (await db.history("TEST", 10))[0]
    assert srow["entry_price"] == 101.8, f"entry_price={srow['entry_price']}, expected 101.8"

    # A real tick inside the placeholder minute clears its synthetic flag.
    late2: dict[int, tuple[dict, float]] = {}
    assert fm._handle_tick(state, base + 60, 103.0, late2) is None
    assert "synthetic" not in state.current
    await fm._flush_late_updates(state, late2)

    # Its boundary finalize now produces a real second signal.
    finalized2 = state.current
    state.current = {
        "ts": base + 120, "open": 103.0, "high": 103.0, "low": 103.0,
        "close": 103.0, "synthetic": True,
    }
    await fm._finalize_candle(state, finalized2)
    assert len(signals) == 2, f"expected 2 signals, got {len(signals)}"
    row2 = await db.get_candle("TEST", base + 60)
    assert row2["synthetic"] == 0

    # --- scenario 3: double finalize of the same candle is a no-op signal-wise
    await fm._finalize_candle(state, finalized2)
    assert len(signals) == 2, "a second finalize of the same candle must not fire a signal"

    # --- scenario 4: a candle finalized a minute late fires no stale signal
    stale = {"ts": base - 120, "open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0}
    await fm._finalize_candle(state, stale)
    assert len(signals) == 2, "a stale candle must not fire a signal"

    # --- scenario 5: repair migration re-grades race leftovers + dedupes stats
    await db.save_candle("TEST", base + 180, 1.0, 1.0, 1.0, 1.0, synthetic=True)
    sid = await db.insert_signal("TEST", "CALL", base + 180, base + 240, None, "doji_reversal", 1.0)
    await db.grade_signal(sid, 2.0, "WIN")  # graded against a synthetic outcome -> race leftover

    for _ in range(2):  # two WIN rows for the SAME entry minute (duplicates)
        d = await db.insert_signal("TEST", "CALL", base + 300, base + 360, None, "hammer", 2.0)
        await db.grade_signal(d, 2.5, "WIN")
    d3 = await db.insert_signal("TEST", "CALL", base + 420, base + 480, None, "hammer", 2.0)
    await db.grade_signal(d3, 1.5, "LOSS")

    await db.init_db()  # re-runs the migration: repairs + one-time dedupe rebuild

    regraded = next(r for r in await db.history("TEST", 20) if r["id"] == sid)
    assert regraded["result"] == "DRAW" and regraded["close_price"] is None, \
        f"race-leftover signal should be DRAW, got {regraded['result']}"

    wins, losses = await db.pattern_stats("hammer", "TEST")
    assert (wins, losses) == (1, 1), \
        f"duplicate WIN pair must collapse to one sample, got ({wins}, {losses})"
    dwins, dlosses = await db.pattern_stats("doji_reversal", "TEST")
    assert (dwins, dlosses) == (0, 0), "re-graded DRAW must drop out of stats"

    # The two feed signals stayed PENDING and untouched.
    pending = [r for r in await db.history("TEST", 20) if r["result"] == "PENDING"]
    assert len(pending) == 2

    print("ALL FEED-FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
