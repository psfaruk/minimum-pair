import time
from dataclasses import dataclass, field
from typing import Any

from app import candle_reaction, config, db, htf, microstructure, pattern_miner, patterns, regime, weights

# 2026-09 (confluence v3) — the veto system is gone. The old engine let
# microstructure (a rough blended read) and candle-reaction (a single
# wick) OVERRIDE the whole weighted vote pool after the fact, and routed
# every gate failure into _fallback_decision(), which fired a best-guess
# signal on every candle of every pair (~23k rows/day, most of them noise).
# Both mechanisms are overrides in the user's sense of the word: a
# decision the confluence itself did not make.
#
# The engine is now pure confluence: every read votes, the pool decides,
# and a signal exists ONLY when the gates below all pass. Anything less
# is silence — no filler, no fallback tier, no overrides.
#
# The microstructure read is a VOTE like every other (family
# "microstructure"), so its information still reaches the pool — it just
# can no longer block or replace the pool's answer.

PATTERN_PERF_CACHE_TTL = 55.0

# How much graded history a source (or confluence signature) needs before
# its measured rate is reported as confidence. Below this the app says
# nothing rather than quoting a made-up prior. A 25-sample rate has a
# ±20-point confidence interval and was gating "confirmed" on noise.
CONFIDENCE_MIN_SAMPLES = 40

# Only relevant when MIN_CONFLUENCE_STRATEGIES is configured as 1: a lone
# vote can then only confirm the signal if its source has actually EARNED
# a strong weight on this pair (measured well above coin-flip). With the
# default of 2+ agreeing families this rule never triggers.
LONE_VOTE_MIN_WEIGHT = 0.80

# microstructure only votes when its blended read clears this strength —
# below it the score is noise.
MICRO_VOTE_FLOOR = 0.15

TIER_CONFIRMED = "confirmed"

# The microstructure read gets its own strategy family: it is an
# independent composite of colour/body/wick/streak momentum, not an
# oscillator or a pattern-library member.
FAMILY_MICRO = "microstructure"

# ---------------------------------------------------------------------------
# Idea clustering — the anti-double-counting layer
#
# The vote pool must not count every detector as independent evidence.
# One physical event — "price bounced off the floor" — fires rsi_oversold
# AND bb_lower_bounce AND near_support AND fractal/ema/bb/round-number
# rejection_bottom AND hammer, all on the SAME candle. Five
# "confirmations", one idea. In a downtrend those five correlated
# reversion votes systematically outvoted the single honest trend vote,
# which is exactly how an anti-trend call got presented as a strong
# confirmed signal.
#
# The fix: votes are grouped by (strategy family, direction). The FIRST
# vote in a cluster carries its full earned weight; every ADDITIONAL vote
# in the same cluster contributes a decaying share (cap / (cap + already
# contributed)) — one idea, one vote, no matter how many detectors saw
# it. A source that has measurably earned a heavy weight is never capped
# below its own record, because its first-in-cluster contribution is
# always its full weight. Confirmations are counted as distinct IDEAS
# (families) on the winning side, not raw detector counts.
# ---------------------------------------------------------------------------
CLUSTER_CAP_MULTIPLE = 2.3  # × BASE_WEIGHT — where a cluster's extra votes saturate

# (source, pair) -> (wins, losses). Per pair, because a pattern that
# works on EUR/USD has no claim on USD/BDT OTC — they aren't the same
# market and the old global tally quietly assumed they were.
_pattern_perf_cache: dict[tuple[str, str], tuple[int, int]] = {}
# (signature, pair) -> (wins, losses) — the record of each CONFLUENCE
# (a named strategy the engine composed itself) on that pair. This is the
# "the app makes its own strategies and fires only the best ones" layer:
# once a signature has enough graded outcomes, its own measured win rate
# gates whether it may fire again.
_signature_perf_cache: dict[tuple[str, str], tuple[int, int]] = {}
_perf_cache_ts: float = 0.0

# Latest regime read per pair, refreshed on EVERY finalized candle (not
# only when a signal fires) so /api/status stays honest even while the
# engine is silently waiting for a confluence.
last_regime_by_pair: dict[str, str] = {}


async def _refresh_perf_caches() -> None:
    global _pattern_perf_cache, _signature_perf_cache, _perf_cache_ts
    now = time.time()
    if now - _perf_cache_ts < PATTERN_PERF_CACHE_TTL:
        return
    _pattern_perf_cache = await db.all_pattern_stats()
    _signature_perf_cache = await db.all_signal_stats()
    _perf_cache_ts = now


def source_record(pair: str, source: str) -> tuple[int, int]:
    return _pattern_perf_cache.get((source, pair), (0, 0))


def signature_record(pair: str, signature: str) -> tuple[int, int]:
    return _signature_perf_cache.get((signature, pair), (0, 0))


def confluence_signature(direction: str, regime_name: str, families: list[str]) -> str:
    """The stable name of a confluence-strategy: which direction, under
    which market regime, which strategy families agreed. Sources inside a
    family churn (mined keys retrain, detectors fire alternately) — the
    FAMILY SET is the durable identity of the strategy."""
    return f"{direction}|{regime_name}|{'+'.join(families)}"


@dataclass
class Vote:
    direction: str  # "CALL" or "PUT"
    weight: float
    source: str
    family: str = ""  # regime.FAMILY_* / FAMILY_MICRO / "" (miner — own family)


@dataclass
class Decision:
    direction: str
    confidence: float | None  # None until there's enough graded history to mean anything
    confirmations: int  # distinct strategy families on the winning side
    sources: list[str]
    tier: str  # always TIER_CONFIRMED — a Decision only exists when every gate passed
    regime: str = regime.REGIME_NEUTRAL  # market regime the vote pool was weighed under
    all_votes: list[tuple[str, str]] = field(default_factory=list)  # EVERY vote, both sides
    families: list[str] = field(default_factory=list)  # sorted families that agreed
    signature: str = ""  # the confluence's stable name (learned per pair)


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


def _anchor_displacement(candles: list[dict[str, Any]], lookback: int = 60) -> float:
    """How many ATRs the close sits above(+) / below(-) its recent mean.

    The strongest single simple edge measured on OTC-style feeds
    (tools/measure_edges.py: 65% fade win rate at |disp| >= 1 ATR on the
    mean-reverting generator) — and symmetrically, displacement
    CONTINUATION is what wins inside real trends (68% on the trending
    generator). The regime layer decides which way this reads.
    """
    window = candles[-lookback:]
    if len(window) < 20:
        return 0.0
    closes = [c["close"] for c in window]
    mean = sum(closes) / len(closes)
    atr = sum(max(c["high"] - c["low"], 1e-12) for c in candles[-14:]) / 14
    if atr <= 0:
        return 0.0
    return (closes[-1] - mean) / atr


def _anchor_votes(regime_read: dict[str, Any], disp: float) -> list[Vote]:
    """Displacement read, tagged with the family the regime says is live:

    - range/neutral regime: price stretched >= 1 ATR from its mean gets
      FADED (mean reversion is the theory the market is exhibiting);
    - trend regime (strength >= 0.25): the same displacement is FOLLOWED
      (in a real trend, distance from the mean is momentum, not stretch).

    One physical quantity, two regime-conditional theories — which is
    exactly how a discretionary trader reads it.
    """
    votes: list[Vote] = []
    magnitude = abs(disp)
    if magnitude < 1.0:
        return votes
    fade_dir = "CALL" if disp < 0 else "PUT"
    follow_dir = "CALL" if disp > 0 else "PUT"
    if regime_read["regime"] in (regime.REGIME_RANGE, regime.REGIME_NEUTRAL):
        base = weights.BASE_WEIGHT * (0.9 + 0.5 * min(magnitude - 1.0, 1.2))
        if regime_read["regime"] == regime.REGIME_NEUTRAL:
            base *= 0.6  # regime unsure — the read is worth less
        votes.append(Vote(fade_dir, base, "anchor_fade", regime.FAMILY_REVERSION))
    elif regime_read["regime"] == regime.REGIME_TREND and regime_read["strength"] >= 0.25:
        weight = weights.BASE_WEIGHT * (0.8 + 0.4 * min(magnitude - 1.0, 1.2))
        votes.append(Vote(follow_dir, weight, "anchor_follow", regime.FAMILY_TREND))
    return votes


def _streak_exhaustion_vote(candles: list[dict[str, Any]]) -> Vote | None:
    """Overextension fade — after N same-colour candles in a row, a
    mean-reverting (OTC-style) feed increasingly pays the opposite side.

    This is the single most reliable simple edge on OTC-style feeds and
    the engine had no dedicated source for it: microstructure blended a
    streak component at 10% weight, far too little to matter. The vote
    scales with the streak and belongs to the reversion family, so in a
    genuine trend the regime layer demotes it (fading a real trend is
    how accounts die) while in a range it is boosted.
    """
    if len(candles) < 3:
        return None
    last = candles[-1]
    if last["close"] == last["open"]:
        return None
    up = last["close"] > last["open"]
    streak = 0
    for c in reversed(candles):
        if c["close"] == c["open"] or (c["close"] > c["open"]) != up:
            break
        streak += 1
    if streak < 3:
        return None
    direction = "PUT" if up else "CALL"
    # Strengthened after measuring 64.6% win rate on streaks of 3+ on
    # OTC-style feeds (tools/measure_edges.py); the old 0.8x start
    # undersold the engine's most reliable reversion read.
    weight = min(weights.BASE_WEIGHT * (1.0 + 0.3 * (streak - 3)), weights.BASE_WEIGHT * 1.9)
    return Vote(direction, weight, "streak_exhaustion", regime.FAMILY_REVERSION)


def _htf_trend_vote(context: dict[str, Any]) -> Vote | None:
    """Higher-timeframe alignment — trade WITH the 5-minute leg, not
    against it. One honest vote, strength-scaled, trend family."""
    if context.get("trend") not in ("up", "down") or context.get("strength", 0) < 0.30:
        return None
    direction = "CALL" if context["trend"] == "up" else "PUT"
    weight = weights.BASE_WEIGHT * (0.8 + 0.5 * min(context["strength"], 1.0))
    return Vote(direction, weight, "htf_trend", regime.FAMILY_TREND)


def _fallback_vote(candles: list[dict[str, Any]]) -> Vote:
    """Last-candle-colour direction. NOT a live signal source any more —
    kept purely as a reference baseline that app/backtest.py measures
    (`fallback_color` row), so the numbers show what naive
    colour-following alone would have scored on the same data."""
    import random

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
    await _refresh_perf_caches()
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


def _capped_contributions(votes: list[Vote]) -> list[tuple[Vote, float]]:
    """Applies the idea-cluster cap (see the block comment above).

    Returns (vote, capped_contribution) pairs. The first vote in a
    cluster keeps its full weight; later votes in the same cluster decay
    as cap / (cap + already_contributed). A single heavy source is never
    demoted — the cap only throttles the pile-up of correlated votes
    BEHIND it.
    """
    cap = weights.BASE_WEIGHT * CLUSTER_CAP_MULTIPLE
    contributed: dict[tuple[str, str], float] = {}
    out: list[tuple[Vote, float]] = []
    for v in votes:
        key = (v.family or "own", v.direction)
        cur = contributed.get(key, 0.0)
        share = v.weight if cur <= 0 else v.weight * cap / (cap + cur)
        contributed[key] = cur + share
        out.append((v, share))
    return out


async def evaluate(pair: str, candles: list[dict[str, Any]], ind: dict[str, Any]) -> Decision | None:
    """Confluence-only decision — confluence v3 (2026-09).

    Returns a Decision ONLY when every gate below passes. Any other
    outcome is silence: no fallback tier, no filler signal, no best
    guess. The gates, in order:

      0. Enough clean history (MIN_HISTORY_CANDLES) — degraded reads
         must never trade.
      1. The weighted pool has a net direction at all.
      2. At least MIN_CONFLUENCE_STRATEGIES distinct strategy FAMILIES
         agree on the winning side ("বেশ কয়েকটি স্ট্রাটেজি একমত").
         Correlated detectors inside one family are one idea (cluster
         cap), so this counts independent theories, not repeat voters.
      3. The winning side carries at least QUALITY_FLOOR of the total
         vote weight — real dominance, not a coin-flip split.
      4. Regime alignment ("এটা অবস্থান বোঝে সিদ্ধান্ত নিবে"): when the
         regime read is confident (trend or range), the winning side
         must include the family whose theory matches that regime.
      5. Measured confidence: once this exact confluence (signature) or
         the source mix has >= CONFIDENCE_MIN_SAMPLES graded outcomes on
         this pair, its shrunk win rate must be >= MIN_CONFIDENCE — a
         strategy that has proven itself unreliable here goes silent.
         While unmeasured, the bootstrap rule demands structural
         agreement >= BOOTSTRAP_AGREEMENT instead.

    There are deliberately NO post-hoc overrides: no veto can flip or
    block the pool's answer, no fallback path invents a direction.
    """
    clean_n = sum(1 for c in candles if not c.get("synthetic"))
    if clean_n < config.MIN_HISTORY_CANDLES:
        last_regime_by_pair[pair] = regime.detect(candles)["regime"] if candles else regime.REGIME_NEUTRAL
        return None

    pattern_miner.maybe_retrain(pair, candles)

    # evaluate() is only ever called once per finalized candle (see
    # feed.py's _finalize_candle), so there's no risk of double-firing
    # within a candle.
    cr_hit = candle_reaction.detect(candles, ind)

    # THE market read comes first: is this pair currently travelling
    # (trend) or oscillating (range)? Every vote is then weighed by how
    # well its theory matches that condition — see app/regime.py.
    regime_read = regime.detect(candles)
    last_regime_by_pair[pair] = regime_read["regime"]
    fade = regime.fade_last_move(regime_read)

    # Zoomed-out read: what is the 5-minute leg doing? Adds one
    # higher-timeframe vote to the pool (trend family).
    htf_read = htf.htf_context(candles)

    votes = _indicator_votes(ind)
    votes.extend(_pattern_votes(candles))
    cr_vote = _candle_reaction_vote(cr_hit)
    if cr_vote:
        votes.append(cr_vote)
    miner_vote = _pattern_miner_vote(pair, candles)
    if miner_vote:
        votes.append(miner_vote)
    micro = microstructure.score(candles, fade=fade)
    if micro["direction"] is not None and micro["strength"] >= MICRO_VOTE_FLOOR:
        votes.append(
            Vote(
                micro["direction"],
                weights.BASE_WEIGHT * (0.75 + 0.5 * min(micro["strength"], 1.0)),
                microstructure.PATTERN_NAME,
                FAMILY_MICRO,
            )
        )
    streak_vote = _streak_exhaustion_vote(candles)
    if streak_vote:
        votes.append(streak_vote)
    htf_vote = _htf_trend_vote(htf_read)
    if htf_vote:
        votes.append(htf_vote)
    disp = _anchor_displacement(candles)
    votes.extend(_anchor_votes(regime_read, disp))

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

    # One idea, one vote: cap correlated pile-ups BEFORE aggregating.
    capped = _capped_contributions(votes)

    call_weight = sum(c for v, c in capped if v.direction == "CALL")
    put_weight = sum(c for v, c in capped if v.direction == "PUT")

    net_direction = None
    if call_weight > put_weight:
        net_direction = "CALL"
    elif put_weight > call_weight:
        net_direction = "PUT"

    contributing = [
        (v, c) for v, c in capped if net_direction is not None and v.direction == net_direction and c > 0.02
    ]
    families = sorted({(v.family or "own") for v, _ in contributing})

    total_capped = sum(c for _, c in capped) or 1.0
    structural_weight = min(sum(c for _, c in contributing) / total_capped, 1.0)

    # --- GATE 1: multi-strategy confluence ---------------------------------
    if net_direction is None or not contributing:
        return None
    if len(families) < config.MIN_CONFLUENCE_STRATEGIES:
        return None

    # Only meaningful when MIN_CONFLUENCE_STRATEGIES is configured as 1:
    # a lone family must have measurably EARNED the right to confirm.
    if len(families) == 1:
        cluster = families[0]
        top_source_weight = max(
            (v.weight for v, _ in contributing if (v.family or "own") == cluster),
            default=0.0,
        )
        if top_source_weight < LONE_VOTE_MIN_WEIGHT:
            return None

    # --- GATE 2: agreement dominance ---------------------------------------
    if structural_weight < config.QUALITY_FLOOR:
        return None

    # --- GATE 3: regime alignment ------------------------------------------
    # A direction supported ONLY by families arguing the opposite of the
    # theory the market is currently exhibiting (all trend votes inside a
    # confirmed range, or all reversion votes inside a confirmed trend)
    # is the classic wrong-side read — measured at ~30-40% win rate on
    # the OTC-style feed. This is a pure gate now: it cannot flip the
    # direction or fire anything else; the engine just stays silent.
    if (
        regime_read["regime"] in (regime.REGIME_TREND, regime.REGIME_RANGE)
        and regime_read["strength"] >= config.MIN_REGIME_ALIGNMENT_STRENGTH
    ):
        live_family = (
            regime.FAMILY_TREND
            if regime_read["regime"] == regime.REGIME_TREND
            else regime.FAMILY_REVERSION
        )
        if live_family not in families:
            return None

    sources = [v.source for v, _ in contributing]

    # --- GATE 4: measured confidence ---------------------------------------
    signature = confluence_signature(net_direction, regime_read["regime"], families)
    confidence: float | None = None

    await _refresh_perf_caches()
    sig_wins, sig_losses = signature_record(pair, signature)
    if sig_wins + sig_losses >= CONFIDENCE_MIN_SAMPLES:
        # This exact confluence-strategy has its own graded record on
        # this pair — trust IT above any per-source average.
        confidence = max(0.0, min(1.0, weights.shrunk_rate(sig_wins, sig_losses)))
    else:
        measured = await _measured_win_rate(pair, sources)
        if measured is not None:
            rate, samples = measured
            if samples >= CONFIDENCE_MIN_SAMPLES:
                wins = round(rate * samples)
                confidence = max(0.0, min(1.0, weights.shrunk_rate(wins, samples - wins)))

    if confidence is not None:
        # A confluence that has measured below the bar on this pair is
        # finished — it goes silent instead of degrading into a
        # second-class signal.
        if confidence < config.MIN_CONFIDENCE:
            return None
    else:
        # Bootstrap: nothing measured yet. The structural confluence must
        # be far above the ordinary bar before the engine will speak.
        if structural_weight < config.BOOTSTRAP_AGREEMENT:
            return None

    return Decision(
        direction=net_direction,
        confidence=round(confidence, 3) if confidence is not None else None,
        confirmations=len(families),
        sources=sources,
        tier=TIER_CONFIRMED,
        regime=regime_read["regime"],
        all_votes=[(v.source, v.direction) for v in votes],
        families=families,
        signature=signature,
    )
