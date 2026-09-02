"""Unit tests for the engine fixes (updated for confluence v3, 2026-09).

Covers:
  1. regime.detect — trending series reads "trend", oscillating reads
     "range", driftless noise reads "neutral".
  2. regime.family_multiplier — reversion votes boosted in range and
     demoted in trend (and mirrored for trend votes), never to zero.
  3. indicators bb_squeeze — now relative to the band's own recent
     width history (the old absolute 0.5 test was always true).
  4. recent_fractal_levels — only the most recent swings survive.
  5. decision confluence gates — a lone unmeasured vote stays SILENT
     (no fallback tier exists any more); two agreeing families confirm
     under the bootstrap rule; a measured-bad confluence goes silent;
     a regime-opposed pool goes silent.
  6. signature learning — the evaluator bumps signal_stats, and the
     engine gates on a mature signature's own record.
  7. grading accuracy — double-grading cannot double-count stats; the
     (pair, entry_ts) unique index rejects duplicate signals.
  8. evaluator all-votes grading — outvoted minority sources
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

from app import config, decision, db, evaluator, indicators, regime  # noqa: E402
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


def test_fallback_tier_is_gone():
    """The user requirement is absolute: NO fallback signals, NO
    overrides. A lone unmeasured vote (the old engine's 'confirmed by
    one doji' and later 'fallback with the ensemble direction' cases)
    must now produce NO decision at all — silence, not a filler row."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t3.db"
    asyncio.run(db.init_db())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    # Exactly ONE vote: RSI oversold. All other detectors silenced, the
    # indicator snapshot carries only the RSI field.
    ind = {"rsi": 30.0}

    def _neutral(_candles):
        return {"regime": regime.REGIME_NEUTRAL, "strength": 0.0, "score": 0.0,
                "efficiency_ratio": 0.0, "autocorr": 0.0, "samples": 0}

    with (
        patch.object(decision.config, "MIN_HISTORY_CANDLES", 3),
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
        patch.object(decision.microstructure, "score", return_value={"direction": None, "strength": 0.0}),
        patch.object(decision.regime, "detect", side_effect=_neutral),
        patch.object(decision.htf, "htf_context", return_value={"trend": "flat", "strength": 0.0}),
    ):
        dec = asyncio.run(decision.evaluate("TESTPAIR", mild, ind))
    assert dec is None, f"a lone unmeasured vote must stay silent, got {dec}"
    print("  decision.lone-unmeasured-silent OK")


def test_two_family_confluence_confirms():
    """The heart of the confluence requirement: TWO independent strategy
    families agreeing (reversion via RSI + microstructure read) passes
    the gates and fires a confirmed signal — with confidence=None while
    the confluence is still unmeasured (bootstrap rule: structural
    agreement 1.0 >= BOOTSTRAP_AGREEMENT)."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t5.db"
    asyncio.run(db.init_db())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    ind = {"rsi": 30.0}

    def _neutral(_candles):
        return {"regime": regime.REGIME_NEUTRAL, "strength": 0.0, "score": 0.0,
                "efficiency_ratio": 0.0, "autocorr": 0.0, "samples": 0}

    with (
        patch.object(decision.config, "MIN_HISTORY_CANDLES", 3),
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
        patch.object(decision.regime, "detect", side_effect=_neutral),
        patch.object(decision.htf, "htf_context", return_value={"trend": "flat", "strength": 0.0}),
    ):
        dec = asyncio.run(decision.evaluate("TESTPAIR2", mild, ind))
    assert dec is not None, "two agreeing families must fire under the bootstrap rule"
    assert dec.tier == "confirmed"
    assert dec.direction == "CALL"
    assert dec.confirmations >= 2, f"confirmations={dec.confirmations}"
    assert "mean_reversion" in dec.families and decision.FAMILY_MICRO in dec.families
    assert dec.signature.startswith("CALL|neutral|")
    assert dec.confidence is None  # unmeasured — no invented number
    print("  decision.two-family-confluence OK")


def test_measured_bad_confluence_goes_silent():
    """Once a confluence's source mix has a mature record BELOW the
    confidence bar on this pair, the engine stays silent instead of
    degrading into a weak signal."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t6.db"
    asyncio.run(db.init_db())

    async def seed_bad():
        for _ in range(40):
            await db.bump_pattern_stat("rsi_oversold", "TESTPAIR3", True)
            await db.bump_pattern_stat("rsi_oversold", "TESTPAIR3", False)
            await db.bump_pattern_stat("rsi_oversold", "TESTPAIR3", False)
            await db.bump_pattern_stat("rsi_oversold", "TESTPAIR3", False)
        # microstructure measured at coin-flip
        for _ in range(40):
            await db.bump_pattern_stat("microstructure", "TESTPAIR3", True)
            await db.bump_pattern_stat("microstructure", "TESTPAIR3", False)

    asyncio.run(seed_bad())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    ind = {"rsi": 30.0}

    def _neutral(_candles):
        return {"regime": regime.REGIME_NEUTRAL, "strength": 0.0, "score": 0.0,
                "efficiency_ratio": 0.0, "autocorr": 0.0, "samples": 0}

    with (
        patch.object(decision.config, "MIN_HISTORY_CANDLES", 3),
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
        patch.object(decision.regime, "detect", side_effect=_neutral),
        patch.object(decision.htf, "htf_context", return_value={"trend": "flat", "strength": 0.0}),
    ):
        decision._perf_cache_ts = 0.0
        dec = asyncio.run(decision.evaluate("TESTPAIR3", mild, ind))
    # rsi at 25% shrunk ≈ (10+15)/(40+30) ≈ 0.357; micro at 50% ≈ 0.5 —
    # both below MIN_CONFIDENCE → the whole signal is silent.
    assert dec is None, f"a measured-bad confluence must stay silent, got {dec}"
    print("  decision.measured-bad-silent OK")


def test_regime_opposed_pool_goes_silent():
    """A direction supported only by reversion votes inside a CONFIRMED
    TREND is the classic wrong-side read: the alignment gate must keep
    the engine silent (no flip, no fallback, no veto — just silence)."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t7.db"
    asyncio.run(db.init_db())

    from unittest.mock import patch

    mild = [_candle(1.0, 1.0001), _candle(1.0001, 1.00005), _candle(1.00005, 1.0002)]
    ind = {"rsi": 30.0}

    def _trend(_candles):
        return {"regime": regime.REGIME_TREND, "strength": 0.6, "score": 0.6,
                "efficiency_ratio": 0.5, "autocorr": 0.3, "samples": 2}

    with (
        patch.object(decision.config, "MIN_HISTORY_CANDLES", 3),
        patch.object(decision.patterns, "detect", return_value=[]),
        patch.object(decision.candle_reaction, "detect", return_value=None),
        patch.object(decision.pattern_miner, "predict", return_value=None),
        patch.object(decision.regime, "detect", side_effect=_trend),
        patch.object(decision.htf, "htf_context", return_value={"trend": "flat", "strength": 0.0}),
    ):
        decision._perf_cache_ts = 0.0
        dec = asyncio.run(decision.evaluate("TESTPAIR4", mild, ind))
    assert dec is None, f"regime-opposed confluence must stay silent, got {dec}"
    print("  decision.regime-opposed-silent OK")


def test_lone_vote_cannot_confirm():
    votes = [decision.Vote("CALL", 0.5, "rsi_oversold", regime.FAMILY_REVERSION)]
    # Below LONE_VOTE_MIN_WEIGHT -> silent; at/above -> confirmed
    # happens in evaluate(); here we assert the threshold logic directly.
    assert votes[0].weight < decision.LONE_VOTE_MIN_WEIGHT
    strong = decision.Vote("CALL", decision.LONE_VOTE_MIN_WEIGHT, "measured_source")
    assert strong.weight >= decision.LONE_VOTE_MIN_WEIGHT
    # The default confluence bar is TWO agreeing families.
    assert config.MIN_CONFLUENCE_STRATEGIES >= 2, "confluence default must be >= 2"
    print("  decision.lone-vote-threshold OK")


async def _test_signature_learning():
    """The evaluator must teach signal_stats, and a mature signature's
    own record must gate the engine (the 'best strategy wins' layer)."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t8.db"
    await db.init_db()

    sig = "CALL|neutral|mean_reversion+microstructure"
    await db.insert_signal(
        pair="S", direction="CALL", entry_ts=1000, target_close_ts=1060,
        confidence=None, source="rsi_oversold,microstructure", entry_price=1.1,
        tier="confirmed", signature=sig, families=["mean_reversion", "microstructure"],
    )
    await db.save_candle("S", 1000, 1.1, 1.108, 1.0995, 1.106)  # CALL wins

    signals = await db.pending_signals_due(int(time.time()) + 9999)
    target = next(s for s in signals if s["pair"] == "S")
    graded = []

    async def on_graded(g):
        graded.append(g)

    ok = await evaluator._grade_one(target, on_graded)
    assert ok and graded[0]["result"] == "WIN"
    w, l = await db.signal_stats(sig, "S")
    assert (w, l) == (1, 0), f"signature must learn the win, got ({w}, {l})"

    # Grading the SAME row again must be a no-op — no double counting.
    ok2 = await evaluator._grade_one(target, on_graded)
    assert not ok2, "a second grade of the same signal must not transition"
    w, l = await db.signal_stats(sig, "S")
    assert (w, l) == (1, 0), f"double-grade must not double-count, got ({w}, {l})"
    print("  evaluator.signature-learning OK")


async def _test_unique_index_blocks_duplicates():
    """The (pair, entry_ts) unique index is the hard guarantee behind
    accurate history: the same entry minute can never carry two signals
    (the old feed race graded the same trade twice)."""
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t9.db"
    await db.init_db()

    await db.insert_signal(
        pair="U", direction="CALL", entry_ts=5000, target_close_ts=5060,
        confidence=None, source="rsi_oversold", entry_price=1.0,
    )
    import sqlite3

    try:
        await db.insert_signal(
            pair="U", direction="PUT", entry_ts=5000, target_close_ts=5060,
            confidence=None, source="bb_upper_rejection", entry_price=1.0,
        )
        raise AssertionError("duplicate (pair, entry_ts) insert must fail")
    except sqlite3.IntegrityError:
        pass
    print("  db.unique-index-duplicate-block OK")


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
    test_fallback_tier_is_gone()
    test_two_family_confluence_confirms()
    test_measured_bad_confluence_goes_silent()
    test_regime_opposed_pool_goes_silent()
    test_lone_vote_cannot_confirm()
    asyncio.run(_test_all_votes_graded())
    asyncio.run(_test_legacy_rows_still_grade())
    asyncio.run(_test_signature_learning())
    asyncio.run(_test_unique_index_blocks_duplicates())
    print("ALL ENGINE-FIX TESTS PASSED")
