import random
import time
from dataclasses import dataclass, field
from typing import Any

from app import candle_reaction, config, db, htf, microstructure, pattern_miner, patterns, regime, weights

MICRO_AGREEMENT_FLOOR = 0.30  # microstructure must be this confident to veto
MICRO_FALLBACK_FLOOR = 0.15  # microstructure must be at least this confident to act as fallback

PATTERN_PERF_CACHE_TTL = 55.0

# How much graded history a source needs before its measured rate is
# reported as confidence. Below this the app says nothing rather than
# quoting a made-up prior. (2026-09: raised from 25 — a 25-sample rate
# has a ±20-point confidence interval and was gating "confirmed" on
# noise.)
CONFIDENCE_MIN_SAMPLES = 40

# A lone vote can only confirm the signal if its source has actually
# EARNED a strong weight on this pair. Under the old rule any single
# unmeasured detector firing alone produced a "confirmed" signal — one
# doji on one candle was presented with the same authority as a
# multi-source, regime-aligned, measured-good confluence. A lone source
# still carries weight 0.5 (the unmeasured baseline), so it needs to have
# measured well above coin-flip to confirm by itself.
LONE_VOTE_MIN_WEIGHT = 0.80

TIER_CONFIRMED = "confirmed"
TIER_FALLBACK = "fallback"

# ---------------------------------------------------------------------------
# Idea clustering — the anti-double-counting layer (2026-09)
#
# The vote pool used to count every detector as independent evidence. It
# is not. One physical event — "price bounced off the floor" — fires
# rsi_oversold AND bb_lower_bounce AND near_support AND
# fractal/ema/bb/round-number rejection_bottom AND hammer, all on the
# SAME candle. Five "confirmations", one idea. In a downtrend those five
# correlated reversion votes systematically outvoted the single honest
# trend vote, which is exactly how an anti-trend call got presented as a
# strong confirmed signal.
#
# The fix: votes are grouped by (strategy family, direction). The FIRST
# vote in a cluster carries its full earned weight; every ADDITIONAL vote
# in the same cluster contributes a decaying share (cap / (cap + already
# contributed)) — one idea, one vote, no matter how many detectors saw
# it. A source that has measurably earned a heavy weight is never capped
# below its own record, because its first-in-cluster contribution is
# always its full weight. Confirmations are then counted as distinct
# IDEAS on the winning side, not raw detector counts.
# ---------------------------------------------------------------------------
CLUSTER_CAP_MULTIPLE = 2.3  # × BASE_WEIGHT — where a cluster's extra votes saturate

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
    confirmations: int  # distinct idea clusters on the winning side (2026-09: was raw detector count)
    sources: list[str]
    tier: str  # TIER_CONFIRMED (passed every gate) or TIER_FALLBACK (per-candle guarantee filler)
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
    # 2026-09: strengthened — measured at 64.6% win rate on streaks of 3+
    # on OTC-style feeds (tools/measure_edges.py); the old 0.8x start
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
    """Last-candle-colour direction. Used two ways: (1) as the very last
    resort inside evaluate()'s fallback path, when literally nothing else
    fired for this candle — see TIER_FALLBACK; (2) as a reference
    baseline by app/backtest.py's `fallback_color` row, measuring what
    naive colour-following alone would have scored, for comparison
    against sources that actually earned their weight."""
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
    # Two requirements that pull in opposite directions, both explicit
    # user asks: every pair must show a signal on every candle ("প্রত্যেক
    # ক্যান্ডেল এ সিগন্যাল আসতে হবে"), AND that guarantee must never again
    # look like the old undifferentiated coin-flip flood ("এত পরিমাণে
    # প্রেডিকশন হয় যা অগ্রহণযোগ্য"). Reconciled the same way as before:
    # the guarantee stays absolute, but every gate failure now routes
    # through _fallback_decision() and is tagged TIER_FALLBACK, honestly
    # distinct from TIER_CONFIRMED — poll `tier=confirmed` for only the
    # gate-passing signals.
    #
    # No `ind["ready"]` gate needed here: _indicator_votes() degrades to
    # zero votes on an unready `ind` safely (every field access goes
    # through .get()), and the pattern/candle-reaction/miner/htf/streak/
    # microstructure/fallback sources below only need the raw candle
    # list, not the indicator snapshot.
    #
    # Returns None ONLY when there are zero candles (the very first
    # bootstrap call before any history exists). Once at least one
    # candle is present, every code path that would otherwise return
    # nothing routes through _fallback_decision(), which always returns
    # a Decision — that's the per-candle signal guarantee.
    if not candles:
        return None

    pattern_miner.maybe_retrain(pair, candles)

    # evaluate() is only ever called once per finalized candle (see
    # feed.py's _finalize_candle), so there's no risk of double-firing
    # within a candle — no separate cooldown gate is needed on top of
    # that, and the per-candle guarantee means every pair fires every
    # candle with no exceptions, so a cooldown would only ever work
    # against that.

    cr_hit = candle_reaction.detect(candles, ind)

    # THE market read comes first: is this pair currently travelling
    # (trend) or oscillating (range)? Every vote is then weighed by how
    # well its theory matches that condition — see app/regime.py.
    regime_read = regime.detect(candles)
    fade = regime.fade_last_move(regime_read)

    # Zoomed-out read: what is the 5-minute leg doing? Adds one
    # higher-timeframe vote to the pool (trend family) and gives the
    # fallback path a better-than-colour baseline.
    htf_read = htf.htf_context(candles)

    votes = _indicator_votes(ind)
    votes.extend(_pattern_votes(candles))
    cr_vote = _candle_reaction_vote(cr_hit)
    if cr_vote:
        votes.append(cr_vote)
    miner_vote = _pattern_miner_vote(pair, candles)
    if miner_vote:
        votes.append(miner_vote)
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
    conf_clusters = {(v.family or "own") for v, _ in contributing}

    micro = microstructure.score(candles, fade=fade)

    # Theory-opposition check: does the winning side argue the OPPOSITE
    # of the theory the market is currently exhibiting? A direction
    # supported ONLY by trend-family votes inside a confirmed range (or
    # only by reversion votes inside a confirmed trend) is the classic
    # wrong-side read — on the mean-reverting OTC feed those lone
    # ema_trend signals won ~32-40% of the time. When it happens, the
    # fallback path skips the pool's net direction entirely and falls
    # through to reads that match the regime.
    def _theory_opposed() -> bool:
        if regime_read["regime"] not in (regime.REGIME_TREND, regime.REGIME_RANGE):
            pass  # fall through to the HTF opposition check below
        elif regime_read["strength"] >= 0.30 and contributing:
            live = regime.FAMILY_REVERSION if regime_read["regime"] == regime.REGIME_RANGE else regime.FAMILY_TREND
            if all((v.family or "own") != live for v, _ in contributing):
                return True
        # HTF opposition: a direction supported ONLY by reversion-family
        # votes while the 5-minute leg clearly runs the other way is a
        # fade of a live trend — measured at ~30% win rate as a fallback
        # direction (tools/debug_replay.py, trending + mixed feeds).
        # In a range regime the fade IS the theory, so the check is
        # skipped there (the 5-minute leg barely travels on a range feed
        # anyway).
        if (
            regime_read["regime"] != regime.REGIME_RANGE
            and htf_read["trend"] in ("up", "down")
            and htf_read["strength"] >= 0.40
            and contributing
        ):
            htf_dir = "CALL" if htf_read["trend"] == "up" else "PUT"
            if net_direction != htf_dir and all(
                v.family == regime.FAMILY_REVERSION for v, _ in contributing
            ):
                return True
        return False

    opposed = _theory_opposed()

    def _fallback_decision() -> Decision:
        """The per-candle-guarantee filler: some direction, always,
        tagged for exactly what it is. It passes no gate and carries no
        confidence, because there is nothing here to be confident about.

        The direction is the BEST available read, not a coin flip.
        Priority:
          1. the cluster-capped, regime-weighted, measured-weight
             ensemble's own net direction — the best aggregate estimate
             the engine has, even when a gate refused it the confirmed
             tier — UNLESS the winning side argues the opposite of the
             regime's theory (see _theory_opposed), in which case the
             pool's answer is skipped as the wrong-side read it is;
          2. the regime-aware (fade-aware) microstructure read;
          3. a literal wick rejection, if one fired;
          4. the anchor read — price stretched >= 1 ATR from its mean is
             faded when the market isn't in a confirmed trend;
          5. the higher-timeframe trend, if the 5-minute leg is clearly
             travelling — a far better baseline than colour on a
             trending feed;
          6. last-candle colour, only when literally nothing else fired —
             and INVERTED when the regime says the market fades moves
             (on the OTC-style feed, colour-following wins ~45% — the
             fade of it wins ~55%).

        A veto demotes the TIER (the evidence is not confirmed-quality)
        but does not override the direction: the vetoing source already
        had its vote inside the pool — the pool weighed it and moved on.
        """
        if net_direction is not None and contributing and not opposed:
            direction, sources_ = net_direction, [v.source for v, _ in contributing]
        elif micro["direction"] is not None and micro["strength"] >= MICRO_FALLBACK_FLOOR:
            direction, sources_ = micro["direction"], [microstructure.PATTERN_NAME]
        elif cr_hit is not None:
            direction, sources_ = cr_hit[1], [cr_hit[0]]
        elif (
            abs(disp) >= 1.0
            and regime_read["regime"] == regime.REGIME_TREND
            and regime_read["strength"] >= 0.25
        ):
            # In a confirmed trend, displacement is momentum — follow it.
            direction, sources_ = ("CALL" if disp > 0 else "PUT"), ["anchor_follow"]
        elif abs(disp) >= 1.0 and regime_read["regime"] != regime.REGIME_TREND:
            direction, sources_ = ("CALL" if disp < 0 else "PUT"), ["anchor_fade"]
        elif htf_read["trend"] in ("up", "down") and htf_read["strength"] >= 0.45:
            direction, sources_ = ("CALL" if htf_read["trend"] == "up" else "PUT"), ["htf_trend"]
        else:
            fb = _fallback_vote(candles)
            direction = fb.direction
            if fade:
                direction = "CALL" if direction == "PUT" else "PUT"
            sources_ = ["fallback_color_fade" if fade else "fallback_color"]
        return Decision(
            direction=direction,
            confidence=None,
            confirmations=max(1, len(conf_clusters)),
            sources=sources_,
            tier=TIER_FALLBACK,
            regime=regime_read["regime"],
            all_votes=[(v.source, v.direction) for v in votes],
        )

    if not contributing or len(conf_clusters) < config.MIN_CONFIRMATIONS:
        return _fallback_decision()

    # A lone IDEA can only confirm when the evidence behind it has proven
    # itself on this pair (see LONE_VOTE_MIN_WEIGHT). One cluster with
    # five correlated detectors inside is still one unproven idea — the
    # old code read those as five confirmations and stamped "confirmed"
    # on them. An unproven lone idea still fires — as a best-effort
    # fallback carrying the same direction.
    if len(conf_clusters) == 1:
        cluster = next(iter(conf_clusters))
        top_source_weight = max(
            (v.weight for v, _ in contributing if (v.family or "own") == cluster),
            default=0.0,
        )
        if top_source_weight < LONE_VOTE_MIN_WEIGHT:
            return _fallback_decision()

    # Veto: a confident opposing microstructure read. The veto still
    # means "this is not a confirmed call" — it demotes to fallback
    # instead of vanishing, since the guarantee requires something to
    # come back either way.
    if (
        micro["direction"] is not None
        and micro["direction"] != net_direction
        and micro["strength"] >= MICRO_AGREEMENT_FLOOR
    ):
        return _fallback_decision()

    # Veto: the winning side argues the opposite of the regime's theory
    # (see _theory_opposed) — not confirmed-quality evidence, no matter
    # how many correlated detectors piled onto it.
    if opposed:
        return _fallback_decision()

    # Veto: an actual wick-rejection against the proposed direction, even
    # if other sources outweighed it.
    if cr_hit is not None and cr_hit[1] != net_direction:
        return _fallback_decision()

    sources = [v.source for v, _ in contributing]
    total_capped = sum(c for _, c in capped) or 1.0
    structural_weight = min(sum(c for _, c in contributing) / total_capped, 1.0)

    if structural_weight < config.QUALITY_FLOOR:
        return _fallback_decision()

    # Confidence is a measurement or it is nothing. The old formula mixed
    # a hard-coded 0.55 prior with a "how much did the votes agree" score,
    # neither of which knows anything about being right — and it showed:
    # the 0.5 bucket won 39% of the time while the 0.3 bucket won 56%.
    #
    # 2026-09: the rate is now the Beta posterior mean (shrunk toward 50%
    # by weights.PRIOR_STRENGTH pseudo-trades) instead of the raw
    # frequency — a 30-sample 70% rate used to walk in claiming "70%"
    # when its honest interval still covered 55%.
    measured = await _measured_win_rate(pair, sources)
    confidence = None
    if measured is not None:
        rate, samples = measured
        if samples >= CONFIDENCE_MIN_SAMPLES:
            wins = round(rate * samples)
            losses = samples - wins
            confidence = max(0.0, min(1.0, weights.shrunk_rate(wins, losses)))

    # Only gate on confidence once it's real. An unmeasured signal is
    # still a confirmed one — it just doesn't get to claim a number.
    if confidence is not None and confidence < config.MIN_CONFIDENCE:
        return _fallback_decision()

    return Decision(
        direction=net_direction,
        confidence=round(confidence, 3) if confidence is not None else None,
        confirmations=len(conf_clusters),
        sources=sources,
        tier=TIER_CONFIRMED,
        regime=regime_read["regime"],
        all_votes=[(v.source, v.direction) for v in votes],
    )
