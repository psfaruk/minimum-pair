import random
import time
from dataclasses import dataclass
from typing import Any

from app import candle_reaction, config, db, microstructure, pattern_miner, patterns

# Structural-prior patterns/sources get scaled into a vote weight the same
# way: (prior_win_rate - 0.5) * 4, floored at 0.1. A 0.75 prior (candle
# reaction, the strongest source in the spec) lands near weight 1.0; a
# 0.52 prior barely nudges the vote at all.
MICRO_AGREEMENT_FLOOR = 0.30  # microstructure must be this confident to veto
MICRO_FALLBACK_FLOOR = 0.15  # microstructure must be at least this confident to act as fallback

# Backtest against the live signal history (1200+ graded) surfaced several
# sources with a proven, sustained losing bias (e.g. rsi_overbought at
# 33% over 21 samples) that nothing was stopping from firing again. This
# is a live-updating version of the same idea the self-learned pattern
# miner already applies to mined sequences: once a source has enough
# track record to actually mean something, stop trusting it if it's
# demonstrably below breakeven. Deliberately a simple threshold rather
# than a Wilson-bound test here — that rigor matters for the miner
# because it's testing hundreds of candidate sequences at once (real
# multiple-testing risk); this is re-checking ~20 fixed, already-existing
# indicators, so a plain threshold with a reasonable sample floor is
# proportionate.
LEARN_MIN_SAMPLES = 15
LEARN_SUPPRESS_BELOW = 0.42
PATTERN_PERF_CACHE_TTL = 55.0

_pattern_perf_cache: dict[str, tuple[int, int]] = {}
_pattern_perf_cache_ts: float = 0.0


async def _refresh_pattern_perf_cache() -> None:
    global _pattern_perf_cache, _pattern_perf_cache_ts
    now = time.time()
    if now - _pattern_perf_cache_ts < PATTERN_PERF_CACHE_TTL:
        return
    rows = await db.pattern_performance()
    _pattern_perf_cache = {r["pattern"]: (r["wins"], r["losses"]) for r in rows}
    _pattern_perf_cache_ts = now


def _is_confidently_losing(source: str) -> bool:
    stats = _pattern_perf_cache.get(source)
    if not stats:
        return False
    wins, losses = stats
    total = wins + losses
    if total < LEARN_MIN_SAMPLES:
        return False
    return (wins / total) <= LEARN_SUPPRESS_BELOW


@dataclass
class Vote:
    direction: str  # "CALL" or "PUT"
    weight: float
    source: str


@dataclass
class Decision:
    direction: str
    confidence: float
    confirmations: int
    sources: list[str]


async def _drop_learned_losers(votes: list[Vote]) -> list[Vote]:
    await _refresh_pattern_perf_cache()
    return [v for v in votes if not _is_confidently_losing(v.source)]


def _prior_to_weight(prior: float) -> float:
    return max((prior - 0.5) * 4, 0.1)


def _indicator_votes(ind: dict[str, Any]) -> list[Vote]:
    votes: list[Vote] = []

    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi < 35:
            votes.append(Vote("CALL", 1.0, "rsi_oversold"))
        elif rsi > 65:
            votes.append(Vote("PUT", 1.0, "rsi_overbought"))

    dist = ind.get("ema_distance_atr")
    if dist is not None:
        if dist > 0.5:
            votes.append(Vote("CALL", 0.8, "ema_trend_up"))
        elif dist < -0.5:
            votes.append(Vote("PUT", 0.8, "ema_trend_down"))

    pb = ind.get("percent_b")
    if pb is not None:
        if pb <= 0.05:
            votes.append(Vote("CALL", 1.0, "bb_lower_bounce"))
        elif pb >= 0.95:
            votes.append(Vote("PUT", 1.0, "bb_upper_rejection"))

    if ind.get("sma_fresh_cross"):
        direction = "CALL" if ind.get("sma_cross") == "bullish" else "PUT"
        votes.append(Vote(direction, 1.2, "sma_crossover"))

    close = ind.get("close")
    atr = ind.get("atr14")
    resistance, support = ind.get("resistance"), ind.get("support")
    if close is not None and atr:
        if resistance is not None and (resistance - close) < 0.3 * atr:
            votes.append(Vote("PUT", 0.9, "near_resistance"))
        if support is not None and (close - support) < 0.3 * atr:
            votes.append(Vote("CALL", 0.9, "near_support"))

    return votes


def _pattern_votes(candles: list[dict[str, Any]]) -> list[Vote]:
    return [Vote(direction, _prior_to_weight(prior), name) for name, direction, prior in patterns.detect(candles)]


def _candle_reaction_vote(hit: tuple[str, str, float] | None) -> Vote | None:
    if hit is None:
        return None
    name, direction, prior = hit
    return Vote(direction, _prior_to_weight(prior), name)


def _pattern_miner_vote(pair: str, candles: list[dict[str, Any]]) -> Vote | None:
    hit = pattern_miner.predict(pair, candles)
    if hit is None:
        return None
    name, direction, win_rate = hit
    return Vote(direction, _prior_to_weight(win_rate), name)


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
    return Vote(direction, 0.3, "fallback_color")


async def _measured_win_rate(pair: str, sources: list[str]) -> float | None:
    total_wins = total_losses = 0
    for s in sources:
        wins, losses = await db.pattern_stats(s, pair)
        total_wins += wins
        total_losses += losses
    total = total_wins + total_losses
    if total < 5:
        return None
    return total_wins / total


async def evaluate(pair: str, candles: list[dict[str, Any]], ind: dict[str, Any]) -> Decision | None:
    # Deliberately no `ind["ready"]` gate here: with ALWAYS_SIGNAL on,
    # every pair has to fire on every candle even during the ~20 minutes
    # after startup where a thin bootstrap history hasn't yet reached
    # SMA_SLOW candles. _indicator_votes() degrades to zero votes on an
    # unready `ind` safely (every field access goes through .get()), and
    # the pattern/candle-reaction/miner/microstructure/fallback sources
    # below only need the raw candle list, not the indicator snapshot.
    if not candles:
        return None

    pattern_miner.maybe_retrain(pair, candles)

    # evaluate() is only ever called once per finalized candle (see
    # feed.py's _finalize_candle), so there's no risk of double-firing
    # within a candle — no separate cooldown gate is needed on top of
    # that, and ALWAYS_SIGNAL means every pair fires every candle with
    # no exceptions, so a cooldown would only ever work against that.

    cr_hit = candle_reaction.detect(candles)

    votes = _indicator_votes(ind)
    votes.extend(_pattern_votes(candles))
    cr_vote = _candle_reaction_vote(cr_hit)
    if cr_vote:
        votes.append(cr_vote)
    miner_vote = _pattern_miner_vote(pair, candles)
    if miner_vote:
        votes.append(miner_vote)

    votes = await _drop_learned_losers(votes)

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
    used_fallback = False

    if not contributing or len(contributing) < config.MIN_CONFIRMATIONS:
        if config.ALWAYS_SIGNAL:
            # Guaranteed path: always produces *some* vote, so every pair
            # fires on every candle. None of the quality/agreement gates
            # below apply to it — this is deliberately a weaker, always-on
            # signal, not a confirmed one; its (low) confidence score is
            # informational rather than gating.
            if micro["direction"] is not None and micro["strength"] >= MICRO_FALLBACK_FLOOR:
                fb = Vote(micro["direction"], micro["strength"], microstructure.PATTERN_NAME)
            else:
                fb = _fallback_vote(candles)
            net_direction = fb.direction
            contributing = [fb]
            used_fallback = True
        else:
            return None

    if not used_fallback:
        if conflict and len(contributing) < config.MIN_CONFIRMATIONS:
            # ambiguous market, no fallback strong enough to break the tie
            return None

        # gate: a confident opposing microstructure read vetoes the signal
        if micro["direction"] is not None and micro["direction"] != net_direction and micro["strength"] >= MICRO_AGREEMENT_FLOOR:
            return None

        # gate: an actual wick-rejection against the proposed direction vetoes it,
        # even if other sources outweighed it
        if cr_hit is not None and cr_hit[1] != net_direction:
            return None

    sources = [v.source for v in contributing]
    total_possible = sum(v.weight for v in votes) or 1.0
    structural_weight = min(sum(v.weight for v in contributing) / total_possible, 1.0)

    measured = await _measured_win_rate(pair, sources)
    measured = measured if measured is not None else 0.55

    confidence = 0.6 * measured + 0.4 * structural_weight
    confidence = max(0.0, min(1.0, confidence))

    if not used_fallback:
        if confidence < config.MIN_CONFIDENCE:
            return None
        if structural_weight < config.QUALITY_FLOOR:
            return None

    return Decision(
        direction=net_direction,
        confidence=round(confidence, 3),
        confirmations=len(contributing),
        sources=sources,
    )
