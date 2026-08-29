import random
import time
from dataclasses import dataclass, field
from typing import Any

from app import candle_reaction, config, db, microstructure, pattern_miner, patterns, regime, weights

MICRO_AGREEMENT_FLOOR = 0.30  # microstructure must be this confident to veto

PATTERN_PERF_CACHE_TTL = 55.0

# How much graded history a source needs before its measured rate is
# reported as confidence. Below this the app says nothing rather than
# quoting a made-up prior.
CONFIDENCE_MIN_SAMPLES = 25

# A lone vote can only confirm the signal if its source has actually
# EARNED a strong weight on this pair. Under the old rule any single
# unmeasured detector firing alone produced a "confirmed" signal — one
# doji on one candle was presented with the same authority as a
# multi-source, regime-aligned, measured-good confluence. A lone source
# still carries weight 0.5 (the unmeasured baseline), so it needs to have
# measured well above coin-flip to confirm by itself.
LONE_VOTE_MIN_WEIGHT = 0.80

TIER_CONFIRMED = "confirmed"

# (source, pair) -> (wins, losses). Per pair, because a pattern that
# works on EUR/USD has no claim on USD/BDT OTC — they aren't the same
# market and the old global tally quietly assumed they were.
_pattern_perf_cache: dict[tuple[str, str], tuple[int, int]] = {}
_pattern_perf_cache_ts: float = 0.0


async def _refresh_pattern_perf_cache() -> None:
    global _pattern_perf_cache, _pattern_perf_cache_ts
    now = time.time()
    if now - _pattern_perf_cache_ts < PATTERN_PERF_CACHE_TTL:
        return
    _pattern_perf_cache = await db.all_pattern_stats()
    _pattern_perf_cache_ts = now


def source_record(pair: str, source: str) -> tuple[int, int]:
    return _pattern_perf_cache.get((source, pair), (0, 0))


@dataclass
class Vote:
    direction: str  # "CALL" or "PUT"
    weight: float
    source: str
    family: str = ""  # regime.FAMILY_* — which theory this vote belongs to


@dataclass
class Decision:
    direction: str
    confidence: float | None  # None until there's enough graded history to mean anything
    confirmations: int
    sources: list[str]
    tier: str  # always TIER_CONFIRMED — evaluate() returns None instead of a Decision when the gates fail
    regime: str = regime.REGIME_NEUTRAL  # market regime the vote pool was weighed under
    all_votes: list[tuple[str, str]] = field(default_factory=list)  # EVERY vote, both sides


# Every source is born equal. The weight it actually votes with is
# applied later, in _apply_measured_weights(), from its own record —
# these constructors deliberately have no say in the matter. Each source
# is also tagged with the strategy family its theory belongs to, so the
# regime layer can weigh the two families against the market's actual
# condition instead of letting opposing theories fight blind.
def _indicator_votes(ind: dict[str, Any]) -> list[Vote]:
    votes: list[Vote] = []

    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi < 35:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "rsi_oversold", regime.FAMILY_REVERSION))
        elif rsi > 65:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "rsi_overbought", regime.FAMILY_REVERSION))

    dist = ind.get("ema_distance_atr")
    if dist is not None:
        if dist > 0.5:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "ema_trend_up", regime.FAMILY_TREND))
        elif dist < -0.5:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "ema_trend_down", regime.FAMILY_TREND))

    pb = ind.get("percent_b")
    if pb is not None:
        if pb <= 0.05:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "bb_lower_bounce", regime.FAMILY_REVERSION))
        elif pb >= 0.95:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "bb_upper_rejection", regime.FAMILY_REVERSION))

    # Bollinger squeeze breakout — when the bands have been unusually
    # tight (relative to their own recent history) and price closes
    # outside, the next candle typically continues in the breakout
    # direction.
    if ind.get("bb_squeeze") and ind.get("bb_upper") is not None and ind.get("bb_lower") is not None:
        close = ind.get("close")
        if close is not None:
            if close > ind["bb_upper"]:
                votes.append(Vote("CALL", weights.BASE_WEIGHT, "bb_squeeze_break_up", regime.FAMILY_TREND))
            elif close < ind["bb_lower"]:
                votes.append(Vote("PUT", weights.BASE_WEIGHT, "bb_squeeze_break_down", regime.FAMILY_TREND))

    if ind.get("sma_fresh_cross"):
        direction = "CALL" if ind.get("sma_cross") == "bullish" else "PUT"
        votes.append(Vote(direction, weights.BASE_WEIGHT, "sma_crossover", regime.FAMILY_TREND))

    close = ind.get("close")
    atr = ind.get("atr14")
    resistance, support = ind.get("resistance"), ind.get("support")
    if close is not None and atr:
        if resistance is not None and (resistance - close) < 0.3 * atr:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "near_resistance", regime.FAMILY_REVERSION))
        if support is not None and (close - support) < 0.3 * atr:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "near_support", regime.FAMILY_REVERSION))

    return votes


# Continuation patterns argue the move continues (trend family); every
# other pattern in the library is a reversal argument (reversion family).
_CONTINUATION_PATTERNS = {"marubozu_bull", "marubozu_bear"}


def _pattern_votes(candles: list[dict[str, Any]]) -> list[Vote]:
    return [
        Vote(
            direction,
            weights.BASE_WEIGHT,
            name,
            regime.FAMILY_TREND if name in _CONTINUATION_PATTERNS else regime.FAMILY_REVERSION,
        )
        for name, direction in patterns.detect(candles)
    ]


def _candle_reaction_vote(hit: tuple[str, str] | None) -> Vote | None:
    if hit is None:
        return None
    name, direction = hit
    return Vote(direction, weights.BASE_WEIGHT, name, regime.FAMILY_REVERSION)


def _pattern_miner_vote(pair: str, candles: list[dict[str, Any]]) -> Vote | None:
    hit = pattern_miner.predict(pair, candles)
    if hit is None:
        return None
    name, direction, _win_rate = hit
    # The miner learns its own direction from this pair's sequences, so it
    # belongs to no hand-labelled family — the regime layer passes it
    # through untouched.
    return Vote(direction, weights.BASE_WEIGHT, name, "")


def _fallback_vote(candles: list[dict[str, Any]]) -> Vote:
    """Last-candle-colour direction, used only as a reference baseline by
    app/backtest.py's `fallback_color` row — it measures what naive
    colour-following would have scored, for comparison against sources
    that actually earned their weight. Never used to fire a live signal:
    evaluate() no longer has a fallback path at all."""
    last = candles[-1] if candles else None
    if last is not None and last["close"] != last["open"]:
        direction = "CALL" if last["close"] > last["open"] else "PUT"
    else:
        direction = random.choice(["CALL", "PUT"])
    return Vote(direction, weights.BASE_WEIGHT, "fallback_color")


async def _apply_measured_weights(pair: str, votes: list[Vote]) -> list[Vote]:
    """Replaces each vote's placeholder weight with the one its record on
    this pair has earned.

    This is the only place a weight is decided. Sources with no history
    all weigh the same, so a new pair starts with an honest tie rather
    than a hierarchy someone typed in; sources with history are ranked by
    what they actually did, shrunk toward no-edge so a short lucky run
    can't promote one. Nothing is ever weighted to zero — a silent source
    stops being graded, and would never get the chance to recover.
    """
    await _refresh_pattern_perf_cache()
    return [
        Vote(v.direction, weights.weight_for(*source_record(pair, v.source)), v.source, v.family)
        for v in votes
    ]


async def _measured_win_rate(pair: str, sources: list[str]) -> tuple[float, int] | None:
    """Measured hit rate for this combination of sources, with the sample
    count it rests on.

    Summing wins and losses across sources — the old approach — inflates
    the sample count, because the sources on one signal all describe the
    same trade and win or lose together. The rate is a sample-weighted
    average, but the effective sample size is the best single source's,
    which is the honest floor for correlated evidence.
    """
    rates: list[tuple[float, int]] = []
    for s in sources:
        wins, losses = await db.pattern_stats(s, pair)
        n = wins + losses
        if n:
            rates.append((wins / n, n))
    if not rates:
        return None
    effective_n = max(n for _, n in rates)
    weighted = sum(rate * n for rate, n in rates) / sum(n for _, n in rates)
    return weighted, effective_n


async def evaluate(pair: str, candles: list[dict[str, Any]], ind: dict[str, Any]) -> Decision | None:
    # No `ind["ready"]` gate needed here: _indicator_votes() degrades to
    # zero votes on an unready `ind` safely (every field access goes
    # through .get()), and the pattern/candle-reaction/miner/
    # microstructure sources below only need the raw candle list, not
    # the indicator snapshot. During bootstrap (before SMA_SLOW candles
    # exist) the reduced vote pool simply makes the quality gates below
    # harder to pass, which is the correct behaviour — no signal fires
    # until there's enough to actually judge.
    #
    # Returns None whenever there is nothing worth calling: zero candles
    # (the very first bootstrap call), or any quality gate below fails.
    # There is no filler tier any more — a candle that doesn't earn a
    # confirmed signal produces no signal at all.
    if not candles:
        return None

    pattern_miner.maybe_retrain(pair, candles)

    # evaluate() is only ever called once per finalized candle (see
    # feed.py's _finalize_candle), so there's no risk of double-firing
    # within a candle — no separate cooldown gate is needed on top of
    # that.

    cr_hit = candle_reaction.detect(candles, ind)

    # THE market read comes first: is this pair currently travelling
    # (trend) or oscillating (range)? Every vote is then weighed by how
    # well its theory matches that condition — see app/regime.py.
    regime_read = regime.detect(candles)
    fade = regime.fade_last_move(regime_read)

    votes = _indicator_votes(ind)
    votes.extend(_pattern_votes(candles))
    cr_vote = _candle_reaction_vote(cr_hit)
    if cr_vote:
        votes.append(cr_vote)
    miner_vote = _pattern_miner_vote(pair, candles)
    if miner_vote:
        votes.append(miner_vote)

    votes = await _apply_measured_weights(pair, votes)

    # Regime weighting: a vote's family is scaled toward the market's
    # current condition. In a confirmed range, reversion votes gain up to
    # 1.6x and trend votes fall to 0.4x; in a confirmed trend, mirrored.
    # Bounded away from zero — demoted sources keep voting, keep being
    # graded, and recover when the regime flips.
    if regime_read["strength"] > 0:
        votes = [
            Vote(v.direction, v.weight * regime.family_multiplier(regime_read, v.family), v.source, v.family)
            for v in votes
        ]

    call_weight = sum(v.weight for v in votes if v.direction == "CALL")
    put_weight = sum(v.weight for v in votes if v.direction == "PUT")

    conflict = call_weight > 0 and put_weight > 0
    net_direction = None
    if call_weight > put_weight:
        net_direction = "CALL"
    elif put_weight > call_weight:
        net_direction = "PUT"

    contributing = [v for v in votes if v.direction == net_direction] if net_direction else []

    micro = microstructure.score(candles, fade=fade)

    # Every gate below returns None on failure, not a filler: a candle
    # that doesn't earn a confirmed signal produces no signal at all —
    # only real, gate-passing trading-strategy calls reach the caller.

    if not contributing or len(contributing) < config.MIN_CONFIRMATIONS:
        return None

    if conflict and len(contributing) < config.MIN_CONFIRMATIONS:
        return None  # ambiguous market

    # A lone vote can only confirm when its source has proven itself on
    # this pair (see LONE_VOTE_MIN_WEIGHT). An unmeasured lone vote is
    # not enough on its own.
    if len(contributing) == 1 and contributing[0].weight < LONE_VOTE_MIN_WEIGHT:
        return None

    # Veto: a confident opposing microstructure read.
    if (
        micro["direction"] is not None
        and micro["direction"] != net_direction
        and micro["strength"] >= MICRO_AGREEMENT_FLOOR
    ):
        return None

    # Veto: an actual wick-rejection against the proposed direction, even
    # if other sources outweighed it.
    if cr_hit is not None and cr_hit[1] != net_direction:
        return None

    sources = [v.source for v in contributing]
    total_possible = sum(v.weight for v in votes) or 1.0
    structural_weight = min(sum(v.weight for v in contributing) / total_possible, 1.0)

    if structural_weight < config.QUALITY_FLOOR:
        return None

    # Confidence is a measurement or it is nothing. The old formula mixed
    # a hard-coded 0.55 prior with a "how much did the votes agree" score,
    # neither of which knows anything about being right — and it showed:
    # the 0.5 bucket won 39% of the time while the 0.3 bucket won 56%.
    measured = await _measured_win_rate(pair, sources)
    confidence = None
    if measured is not None:
        rate, samples = measured
        if samples >= CONFIDENCE_MIN_SAMPLES:
            confidence = max(0.0, min(1.0, rate))

    # Only gate on confidence once it's real. An unmeasured signal is
    # still a confirmed one — it just doesn't get to claim a number.
    if confidence is not None and confidence < config.MIN_CONFIDENCE:
        return None

    return Decision(
        direction=net_direction,
        confidence=round(confidence, 3) if confidence is not None else None,
        confirmations=len(contributing),
        sources=sources,
        tier=TIER_CONFIRMED,
        regime=regime_read["regime"],
        all_votes=[(v.source, v.direction) for v in votes],
    )
