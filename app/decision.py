import random
import time
from dataclasses import dataclass
from typing import Any

from app import candle_reaction, config, db, microstructure, pattern_miner, patterns, weights

MICRO_AGREEMENT_FLOOR = 0.30  # microstructure must be this confident to veto
MICRO_FALLBACK_FLOOR = 0.15  # microstructure must be at least this confident to act as fallback

PATTERN_PERF_CACHE_TTL = 55.0

# How much graded history a source needs before its measured rate is
# reported as confidence. Below this the app says nothing rather than
# quoting a made-up prior.
CONFIDENCE_MIN_SAMPLES = 25

TIER_CONFIRMED = "confirmed"
TIER_NOISE = "noise"

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


@dataclass
class Decision:
    direction: str
    confidence: float | None  # None until there's enough graded history to mean anything
    confirmations: int
    sources: list[str]
    tier: str  # TIER_CONFIRMED (passed the gates) or TIER_NOISE (ALWAYS_SIGNAL filler)


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
        Vote(v.direction, weights.weight_for(*source_record(pair, v.source)), v.source)
        for v in votes
    ]


# Every source is born equal. The weight it actually votes with is
# applied later, in _apply_measured_weights(), from its own record —
# these constructors deliberately have no say in the matter.
def _indicator_votes(ind: dict[str, Any]) -> list[Vote]:
    votes: list[Vote] = []

    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi < 35:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "rsi_oversold"))
        elif rsi > 65:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "rsi_overbought"))

    dist = ind.get("ema_distance_atr")
    if dist is not None:
        if dist > 0.5:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "ema_trend_up"))
        elif dist < -0.5:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "ema_trend_down"))

    pb = ind.get("percent_b")
    if pb is not None:
        if pb <= 0.05:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "bb_lower_bounce"))
        elif pb >= 0.95:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "bb_upper_rejection"))

    # Bollinger squeeze breakout — when bands are tight (low width) and
    # price closes outside, the next candle typically continues in the
    # breakout direction.
    if ind.get("bb_squeeze") and ind.get("bb_upper") is not None and ind.get("bb_lower") is not None:
        close = ind.get("close")
        if close is not None:
            if close > ind["bb_upper"]:
                votes.append(Vote("CALL", weights.BASE_WEIGHT, "bb_squeeze_break_up"))
            elif close < ind["bb_lower"]:
                votes.append(Vote("PUT", weights.BASE_WEIGHT, "bb_squeeze_break_down"))

    if ind.get("sma_fresh_cross"):
        direction = "CALL" if ind.get("sma_cross") == "bullish" else "PUT"
        votes.append(Vote(direction, weights.BASE_WEIGHT, "sma_crossover"))

    close = ind.get("close")
    atr = ind.get("atr14")
    resistance, support = ind.get("resistance"), ind.get("support")
    if close is not None and atr:
        if resistance is not None and (resistance - close) < 0.3 * atr:
            votes.append(Vote("PUT", weights.BASE_WEIGHT, "near_resistance"))
        if support is not None and (close - support) < 0.3 * atr:
            votes.append(Vote("CALL", weights.BASE_WEIGHT, "near_support"))

    return votes


def _pattern_votes(candles: list[dict[str, Any]]) -> list[Vote]:
    return [Vote(direction, weights.BASE_WEIGHT, name) for name, direction in patterns.detect(candles)]


def _candle_reaction_vote(hit: tuple[str, str] | None) -> Vote | None:
    if hit is None:
        return None
    name, direction = hit
    return Vote(direction, weights.BASE_WEIGHT, name)


def _pattern_miner_vote(pair: str, candles: list[dict[str, Any]]) -> Vote | None:
    hit = pattern_miner.predict(pair, candles)
    if hit is None:
        return None
    name, direction, _win_rate = hit
    return Vote(direction, weights.BASE_WEIGHT, name)


def _fallback_vote(candles: list[dict[str, Any]]) -> Vote:
    """Guarantees a direction even when every other source (including
    microstructure) is silent, e.g. a perfectly flat candle — needed
    because ALWAYS_SIGNAL means every pair fires on every candle, with
    no exceptions."""
    last = candles[-1] if candles else None
    if last is not None and last["close"] != last["open"]:
        direction = "CALL" if last["close"] > last["open"] else "PUT"
    else:
        direction = random.choice(["CALL", "PUT"])
    return Vote(direction, weights.BASE_WEIGHT, "fallback_color")


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
    # Deliberately no `ind["ready"]` gate here: with ALWAYS_SIGNAL on,
    # every pair has to fire on every candle even during the ~20 minutes
    # after startup where a thin bootstrap history hasn't yet reached
    # SMA_SLOW candles. _indicator_votes() degrades to zero votes on an
    # unready `ind` safely (every field access goes through .get()), and
    # the pattern/candle-reaction/miner/microstructure/fallback sources
    # below only need the raw candle list, not the indicator snapshot.
    #
    # Returns None ONLY when there are zero candles (the very first
    # bootstrap call before any history exists). Once at least one
    # candle is present, every code path that would previously have
    # returned None now routes through _noise_decision(), which always
    # returns a Decision — that's the per-candle signal guarantee.
    if not candles:
        return None

    pattern_miner.maybe_retrain(pair, candles)

    # evaluate() is only ever called once per finalized candle (see
    # feed.py's _finalize_candle), so there's no risk of double-firing
    # within a candle — no separate cooldown gate is needed on top of
    # that, and ALWAYS_SIGNAL means every pair fires every candle with
    # no exceptions, so a cooldown would only ever work against that.

    cr_hit = candle_reaction.detect(candles, ind)

    votes = _indicator_votes(ind)
    votes.extend(_pattern_votes(candles))
    cr_vote = _candle_reaction_vote(cr_hit)
    if cr_vote:
        votes.append(cr_vote)
    miner_vote = _pattern_miner_vote(pair, candles)
    if miner_vote:
        votes.append(miner_vote)

    votes = await _apply_measured_weights(pair, votes)

    call_weight = sum(v.weight for v in votes if v.direction == "CALL")
    put_weight = sum(v.weight for v in votes if v.direction == "PUT")

    conflict = call_weight > 0 and put_weight > 0
    net_direction = None
    if call_weight > put_weight:
        net_direction = "CALL"
    elif put_weight > call_weight:
        net_direction = "PUT"

    contributing = [v for v in votes if v.direction == net_direction] if net_direction else []

    micro = microstructure.score(candles)

    def _noise_decision() -> Decision:
        """The ALWAYS_SIGNAL filler: some direction, always, tagged for
        exactly what it is. It passes no gate and carries no confidence,
        because there is nothing here to be confident about.

        Per the user requirement ("প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ
        সিগন্যাল আসতে হবে" — every pair, every candle, a signal must
        come), this NEVER returns None. A real candle that reached
        _finalize_candle() always produces a signal — either confirmed
        (passed the gates) or noise (filler). The previous `if not
        config.ALWAYS_SIGNAL: return None` gate would silently produce
        nothing for entire minutes, which broke the per-candle promise
        on every ambiguous candle."""
        if micro["direction"] is not None and micro["strength"] >= MICRO_FALLBACK_FLOOR:
            fb = Vote(micro["direction"], micro["strength"], microstructure.PATTERN_NAME)
        else:
            fb = _fallback_vote(candles)
        return Decision(
            direction=fb.direction,
            confidence=None,
            confirmations=1,
            sources=[fb.source],
            tier=TIER_NOISE,
        )

    if not contributing or len(contributing) < config.MIN_CONFIRMATIONS:
        return _noise_decision()

    if conflict and len(contributing) < config.MIN_CONFIRMATIONS:
        return _noise_decision()  # ambiguous market

    # Veto: a confident opposing microstructure read. Previously this
    # returned None outright even with ALWAYS_SIGNAL on, so 43% of
    # candles silently produced nothing while /api/live kept serving the
    # last signal as though it were current. The veto still means "this
    # is not a confirmed call" — it just demotes instead of vanishing.
    if (
        micro["direction"] is not None
        and micro["direction"] != net_direction
        and micro["strength"] >= MICRO_AGREEMENT_FLOOR
    ):
        return _noise_decision()

    # Veto: an actual wick-rejection against the proposed direction, even
    # if other sources outweighed it.
    if cr_hit is not None and cr_hit[1] != net_direction:
        return _noise_decision()

    sources = [v.source for v in contributing]
    total_possible = sum(v.weight for v in votes) or 1.0
    structural_weight = min(sum(v.weight for v in contributing) / total_possible, 1.0)

    if structural_weight < config.QUALITY_FLOOR:
        return _noise_decision()

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
        return _noise_decision()

    return Decision(
        direction=net_direction,
        confidence=round(confidence, 3) if confidence is not None else None,
        confirmations=len(contributing),
        sources=sources,
        tier=TIER_CONFIRMED,
    )
