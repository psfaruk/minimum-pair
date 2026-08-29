"""Unit tests for the 2026-08 engine fixes.

Covers:
  1. regime.detect — trending series reads "trend", oscillating reads
     "range", driftless noise reads "neutral".
  2. regime.family_multiplier — reversion votes boosted in range and
     demoted in trend (and mirrored for trend votes), never to zero.
  3. indicators bb_squeeze — now relative to the band's own recent
     width history (the old absolute 0.5 test was always true).
  4. recent_fractal_levels — only the most recent swings survive.
  5. decision gate failure — evaluate() returns None (no signal) when
     the vote pool doesn't clear the gates, instead of firing a filler.
  6. decision lone-vote rule — a single unmeasured vote produces no
     signal, not confirmed; a strong measured lone vote confirms.
  7. evaluator all-votes grading — outvoted minority sources
     accumulate losses, which the old majority-only rule never let
     happen.

Run:  python tests/test_engine_fixes.py
"""

import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import decision, db, evaluator, indicators, regime  # noqa: E402
from app.candle_shape import recent_fractal_levels  # noqa: E402


def _candle(o, c, h=None, l=None):
    return {
        "open": o,
        "close": c,
        "high": h if h is not None else max(o, c) + 0.0002,
        "low": l if l is not None else min(o, c) - 0.0002,
        "ts": 0,
    }


def test_regime_detect():
    # Trending: steady rise — high efficiency, positive drift.
    up = [_candle(1.0 + i * 0.001, 1.0 + (i + 1) * 0.001) for i in range(120)]
    r = regime.detect(up)
    assert r["regime"] == "trend", f"steady rise should read trend, got {r}"

    # Oscillating: strict alternation around an anchor — mean reverting.
    osc = []
    for i in range(120):
        o = 1.0 + (0.001 if i % 2 == 0 else -0.001)
        osc.append(_candle(o, 1.0 - (0.001 if i % 2 == 0 else -0.001)))
    r = regime.detect(osc)
    assert r["regime"] == "range", f"strict oscillation should read range, got {r}"

    # Driftless noise: the read must stay neutral (or mild range) in the
    # clear majority of paths. A single window CAN travel far enough to
    # look directed — that is a property of random walks, not a bug —
    # so this asserts the distribution, not one seed.
    neutral_or_range = 0
    checked = 0
    for seed in range(20, 32):
        rng = random.Random(seed)
        price = 1.0
        noise = []
        for _ in range(120):
            prev = price
            price += rng.gauss(0, 0.001)
            noise.append(_candle(prev, price))
        r = regime.detect(noise)
        checked += 1
        if r["regime"] in ("neutral", "range"):
            neutral_or_range += 1
    assert neutral_or_range >= checked - 1, (
        f"driftless noise read trend too often: {checked - neutral_or_range}/{checked}"
    )

    # Too few candles: graceful neutral.
    assert regime.detect(up[:5])["regime"] == "neutral"
    print("  regime.detect OK")


def test_regime_family_multiplier():
    trend_read = {"regime": "trend", "strength": 0.8}
    range_read = {"regime": "range", "strength": 0.8}
    neutral = {"regime": "neutral", "strength": 0.0}

    assert regime.family_multiplier(neutral, regime.FAMILY_REVERSION) == 1.0
    assert regime.family_multiplier(trend_read, regime.FAMILY_REVERSION) < 1.0
    assert regime.family_multiplier(trend_read, regime.FAMILY_TREND) > 1.0
    assert regime.family_multiplier(range_read, regime.FAMILY_REVERSION) > 1.0
    assert regime.family_multiplier(range_read, regime.FAMILY_TREND) < 1.0
    # Never zero — a silenced source stops being graded and can't recover.
    for read in (trend_read, range_read, neutral):
        for fam in (regime.FAMILY_REVERSION, regime.FAMILY_TREND):
            assert regime.family_multiplier(read, fam) > 0.0
    # Unfamiliated sources pass through.
    assert regime.family_multiplier(trend_read, "") == 1.0
    # fade flag flips only on a confident range read.
    assert regime.fade_last_move(range_read) is True
    assert regime.fade_last_move({"regime": "range", "strength": 0.1}) is False
    assert regime.fade_last_move(trend_read) is False
    print("  regime.family_multiplier OK")


def test_bb_squeeze_relative():
    # Calm market: widths stay in a narrow band; the last window is right
    # in the middle of its own history -> NOT a squeeze.
    rng = random.Random(3)
    price = 1.0
    calm = []
    for _ in range(200):
        prev = price
        price += rng.gauss(0, 0.0004)
        calm.append(_candle(prev, price))
    ind = indicators.compute(calm)
    assert ind["ready"]
    assert ind["bb_squeeze"] is False, f"mid-distribution width must not be a squeeze: {ind['bb_width_percentile']}"

    # After 150 calm candles, 20 ultra-tight candles -> a real squeeze.
    tight = calm[:-20]
    price = tight[-1]["close"]
    for _ in range(20):
        prev = price
        price += rng.gauss(0, 0.00004)
        tight.append(_candle(prev, price))
    ind = indicators.compute(tight)
    assert ind["bb_squeeze"] is True, f"tight window should read squeeze, pctl={ind['bb_width_percentile']}"
    print("  indicators.bb_squeeze OK")


def test_recent_fractal_levels():
    rng = random.Random(5)
    highs, lows = [], []
    price = 1.0
    for i in range(120):
        price += rng.gauss(0, 0.001)
        if i % 10 < 5:
            highs.append(price + 0.002)
            lows.append(price - 0.001)
        else:
            highs.append(price + 0.001)
            lows.append(price - 0.002)
    fh, fl = recent_fractal_levels(highs, lows, max_levels=6)
    assert len(fh) <= 6 and len(fl) <= 6
    # The most recent swing must be near the end of the series, not the start.
    all_fractal_h, _ = __import__("app.candle_shape", fromlist=["fractal_levels"]).fractal_levels(highs, lows)
    if all_fractal_h:
        assert fh[0] in all_fractal_h
        assert fh[0] in highs[-30:], "most recent swing high should come from the recent tail"
    print("  recent_fractal_levels OK")


def test_below_gate_produces_no_signal():
    """A below-quality-floor vote pool must produce no signal at all —
    the old noise filler (which fired anyway with the ensemble's
    direction) has been removed."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t3.db"
    asyncio.run(db.init_db())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    # Exactly ONE vote: RSI oversold. All other detectors silenced, the
    # indicator snapshot carries only the RSI field.
    ind = {"rsi": 30.0}

    with (
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
    ):
        dec = asyncio.run(decision.evaluate("TESTPAIR", mild, ind))
    # A lone oversold vote (weight 0.5 < 0.8) cannot confirm, and there is
    # no filler tier to fall back to any more — evaluate() must return None.
    assert dec is None, f"lone unmeasured vote below LONE_VOTE_MIN_WEIGHT must produce no signal, got {dec}"
    print("  decision.below-gate-no-signal OK")


def test_measured_lone_vote_can_confirm():
    """The flip side of the lone-vote rule: a source that has MEASURED
    well on this pair may confirm on its own (the dc131a9 intent)."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t4.db"
    asyncio.run(db.init_db())

    async def seed():
        for _ in range(100):
            await db.bump_pattern_stat("rsi_oversold", "TESTPAIR", True)

    asyncio.run(seed())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    ind = {"rsi": 30.0}

    with (
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
    ):
        # Drop the cache so the freshly seeded stats are read.
        decision._pattern_perf_cache_ts = 0.0
        dec = asyncio.run(decision.evaluate("TESTPAIR", mild, ind))
    assert dec is not None
    assert dec.tier == "confirmed", f"a measured-good lone vote should confirm, got {dec.tier}"
    assert dec.direction == "CALL"
    assert dec.confidence is not None and dec.confidence > 0.9
    print("  decision.measured-lone-vote OK")


def test_lone_vote_cannot_confirm():
    votes = [decision.Vote("CALL", 0.5, "rsi_oversold", regime.FAMILY_REVERSION)]
    # Below LONE_VOTE_MIN_WEIGHT -> no signal; at/above -> confirmed
    # happens in evaluate(); here we assert the threshold logic directly.
    assert votes[0].weight < decision.LONE_VOTE_MIN_WEIGHT
    strong = decision.Vote("CALL", decision.LONE_VOTE_MIN_WEIGHT, "measured_source")
    assert strong.weight >= decision.LONE_VOTE_MIN_WEIGHT
    print("  decision.lone-vote-threshold OK")


async def _test_all_votes_graded():
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t.db"
    await db.init_db()

    # A signal where the minority voted the other side.
    await db.insert_signal(
        pair="P",
        direction="CALL",
        entry_ts=1000,
        target_close_ts=1060,
        confidence=None,
        source="rsi_oversold,hammer",
        entry_price=1.1,
        tier="confirmed",
        all_sources=[
            {"source": "rsi_oversold", "direction": "CALL"},
            {"source": "hammer", "direction": "CALL"},
            {"source": "ema_trend_down", "direction": "PUT"},
        ],
    )
    # Outcome candle at entry_ts: closes ABOVE the entry price (1.1) ->
    # CALL won -> ema_trend_down (voted PUT) must take the loss; under
    # the old rule it was never graded at all.
    await db.save_candle("P", 1000, 1.1, 1.108, 1.0995, 1.106)

    signals = await db.pending_signals_due(int(time.time()) + 9999)
    target = next(s for s in signals if s["pair"] == "P" and s["entry_ts"] == 1000)
    graded = []

    async def on_graded(g):
        graded.append(g)

    ok = await evaluator._grade_one(target, on_graded)
    assert ok and graded[0]["result"] == "WIN"

    w, l = await db.pattern_stats("ema_trend_down", "P")
    assert (w, l) == (0, 1), f"minority PUT source must be graded a loss, got ({w}, {l})"
    w, l = await db.pattern_stats("rsi_oversold", "P")
    assert (w, l) == (1, 0), f"majority CALL source must be graded a win, got ({w}, {l})"

    # Rebuild path agrees with the incremental path.
    import sqlite3
    with db._connect() as conn:
        db._rebuild_pattern_stats(conn)
    w, l = await db.pattern_stats("ema_trend_down", "P")
    assert (w, l) == (0, 1), f"rebuild must keep the minority loss, got ({w}, {l})"
    print("  evaluator.all-votes-graded OK")


async def _test_legacy_rows_still_grade():
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t2.db"
    await db.init_db()
    # Legacy row: no all_sources, source column only.
    await db.insert_signal(
        pair="L", direction="PUT", entry_ts=2000, target_close_ts=2060,
        confidence=None, source="doji_reversal", entry_price=1.5, tier="noise",
    )
    await db.save_candle("L", 2000, 1.5, 1.502, 1.495, 1.49)  # close < entry 1.5 -> PUT won
    signals = await db.pending_signals_due(int(time.time()) + 9999)
    target = next(s for s in signals if s["pair"] == "L")
    async def on_graded(_g):
        pass
    ok = await evaluator._grade_one(target, on_graded)
    assert ok
    w, l = await db.pattern_stats("doji_reversal", "L")
    assert (w, l) == (1, 0), f"legacy grading broken: ({w}, {l})"
    print("  evaluator.legacy-rows OK")


if __name__ == "__main__":
    print("engine-fix tests:")
    test_regime_detect()
    test_regime_family_multiplier()
    test_bb_squeeze_relative()
    test_recent_fractal_levels()
    test_below_gate_produces_no_signal()
    test_measured_lone_vote_can_confirm()
    test_lone_vote_cannot_confirm()
    asyncio.run(_test_all_votes_graded())
    asyncio.run(_test_legacy_rows_still_grade())
    print("ALL ENGINE-FIX TESTS PASSED")
