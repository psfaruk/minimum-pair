"""Per-pair market regime detection — the "characterize the market FIRST,
then apply the strategy that matches it" layer.

The decision engine blends two strategy families that are correct in
OPPOSITE market conditions:

  - **mean-reversion** (RSI extremes, Bollinger bounces, wick rejections,
    S/R fades) — correct when the pair oscillates around a level;
  - **trend-following** (EMA/SMA alignment, marubozu continuation, squeeze
    breakouts) — correct when the pair is genuinely travelling.

Before this module existed the engine never asked which condition it was
in. The two families simply voted against each other, and per-(source,
pair) weights — which learn a source's AVERAGE performance across both
conditions — could not separate them. In a trend, reversal votes fire
repeatedly against the move (RSI stays "oversold" all the way down); in
a range, trend votes enter after the move is already exhausted. That
mixing was the single largest source of wrong-direction signals.

Two independent, complementary reads of the recent close series:

  1. **Kaufman efficiency ratio** — net displacement / path length. A
     random walk over N steps scores ≈ 1/√N (~0.09 here); a directed
     move scores toward 1; a choppy oscillation scores near 0.
  2. **Lag-1 autocorrelation of close-to-close returns.** Negative =
     reversals are self-reinforcing (range); positive = moves persist
     (trend); zero = driftless.

Both are cheap (O(window)), need no indicator history beyond closes, and
fail gracefully to "neutral" on short or flat input.

The output feeds `family_multiplier()`, which scales each vote family's
weight toward the regime's strengths — never to zero (a silenced source
stops being graded and can never recover, the trap documented in
weights.py).
"""
from typing import Any

Candle = dict[str, Any]

REGIME_TREND = "trend"
REGIME_RANGE = "range"
REGIME_NEUTRAL = "neutral"

FAMILY_REVERSION = "mean_reversion"
FAMILY_TREND = "trend_following"

# Candles of closed history the read is taken over (~2 hours on 1-min).
LOOKBACK = 120

# Efficiency-ratio calibration. ER_MID is where a driftless random walk
# lands for this window (1/sqrt(120) ≈ 0.09); ER_TREND is where a
# genuinely directed market lands. Between them sits "no clean opinion".
ER_MID = 0.10
ER_TREND = 0.30
# |lag-1 autocorrelation| that counts as fully opinionated.
AC_SCALE = 0.15

# |score| at or beyond which a regime is declared.
DECLARE_THRESHOLD = 0.25

# A "range" declaration needs the autocorrelation to be negative by more
# than sample noise: the sample ACF of a driftless walk has std ≈ 1/√N
# (≈ 0.09 at N=120), so demanding ac ≤ -0.04 cuts the false-range rate
# on pure noise to ~a tenth while real mean-reverting feeds (ac ≈ -0.2
# and below) still clear it easily.
RANGE_MIN_AUTOCORR = 0.04

# How hard the family weights swing. At full regime strength a matching
# family is boosted to 1.6x and an opposing family demoted to 0.4x —
# bounded well away from zero so demoted sources keep voting, keep being
# graded, and can recover if the regime flips (see weights.py).
MULT_SPAN = 0.6
# Regime strength at which the multiplier reaches full span.
FULL_STRENGTH_AT = 0.67


def _efficiency_ratio(closes: list[float]) -> float:
    """Net displacement over path length — 0 for pure chop, ~1/√N for a
    driftless walk, toward 1 for a directed move."""
    if len(closes) < 3:
        return 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    net = abs(closes[-1] - closes[0])
    if path <= 0:
        return 0.0
    return net / path


def _lag1_autocorr(returns: list[float]) -> float:
    """Lag-1 autocorrelation of the return series. Negative = an up move
    tends to be followed by a down move (mean reversion); positive =
    moves persist (trend).

    A numerically-constant series (every return identical to within
    float noise — a perfectly steady synthetic ramp, a frozen feed) has
    no meaningful autocorrelation: the float residue in the variance
    would amplify to ±1 and masquerade as a strong read. Those return 0
    and let the efficiency ratio speak for them."""
    n = len(returns)
    if n < 10:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns)
    if var <= 0:
        return 0.0
    std = (var / n) ** 0.5
    if std <= 1e-9 * (abs(mean) + 1e-12):
        return 0.0
    cov = sum((returns[i] - mean) * (returns[i - 1] - mean) for i in range(1, n))
    # 1/n denominators on both — the ratio is the standard sample ACF.
    return (cov / n) / (var / n)


def detect(candles: list[Candle]) -> dict[str, Any]:
    """Reads the regime off the most recent closed candles.

    Accepts the same cleaned (synthetic-stripped) ascending candle list
    the rest of the engine uses; strips any synthetic leftovers again
    defensively. Never raises on short input — returns a neutral read.
    """
    clean = [c for c in candles if not c.get("synthetic")][-LOOKBACK:]
    empty = {"regime": REGIME_NEUTRAL, "strength": 0.0, "score": 0.0,
             "efficiency_ratio": 0.0, "autocorr": 0.0, "samples": 0}
    if len(clean) < 15:
        return empty

    closes = [c["close"] for c in clean]
    returns = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    er = _efficiency_ratio(closes)
    ac = _lag1_autocorr(returns)

    er_norm = max(-1.0, min(1.0, (er - ER_MID) / (ER_TREND - ER_MID)))
    ac_norm = max(-1.0, min(1.0, ac / AC_SCALE))

    # Two independent reads, weighted: the efficiency ratio answers "how
    # directed is the path", the autocorrelation answers "do moves
    # persist". Agreement between them is the regime; disagreement
    # cancels toward neutral, which is the honest answer when the two
    # lenses disagree.
    score = 0.55 * er_norm + 0.45 * ac_norm

    # Guardrails against the two lenses' blind spots, calibrated on
    # driftless random walks (tools/regime_calibration.py):
    #   - a "range" call additionally needs negative autocorrelation —
    #     low ER alone fires too often because ER is floored at 0 and
    #     its normalized scale is asymmetric;
    #   - a "trend" call additionally needs a genuinely directed path —
    #     ER alone can spike on a lucky walk.
    if score >= DECLARE_THRESHOLD and er_norm >= 0.35:
        regime = REGIME_TREND
    elif score <= -DECLARE_THRESHOLD and ac <= -RANGE_MIN_AUTOCORR:
        regime = REGIME_RANGE
    else:
        regime = REGIME_NEUTRAL

    return {
        "regime": regime,
        "strength": round(min(abs(score), 1.0), 3),
        "score": round(score, 3),
        "efficiency_ratio": round(er, 4),
        "autocorr": round(ac, 4),
        "samples": len(returns),
    }


def family_multiplier(regime: dict[str, Any], family: str) -> float:
    """The weight multiplier a vote of this strategy family earns under
    this regime.

    A mean-reversion vote in a confirmed range is exactly the theory the
    market is currently rewarding — boosted. The same vote in a confirmed
    trend is the theory the market is currently punishing — demoted, but
    never silenced. Unfamiliated sources (the miner learns its own
    direction) pass through at 1.0.
    """
    if family not in (FAMILY_REVERSION, FAMILY_TREND):
        return 1.0
    r = regime.get("regime")
    if r not in (REGIME_TREND, REGIME_RANGE):
        return 1.0
    span = MULT_SPAN * min(regime.get("strength", 0.0) / FULL_STRENGTH_AT, 1.0)
    agrees = (r == REGIME_TREND and family == FAMILY_TREND) or (
        r == REGIME_RANGE and family == FAMILY_REVERSION
    )
    return 1.0 + span if agrees else 1.0 - span


def fade_last_move(regime: dict[str, Any]) -> bool:
    """Whether the market is currently mean-reverting strongly enough that
    last-candle momentum should be faded rather than followed.

    This is what the ALWAYS_SIGNAL fallback needs: on a range-regime OTC
    feed, following the last candle's colour is systematically wrong —
    the colour-fade is the theory the feed is actually exhibiting.
    """
    return (
        regime.get("regime") == REGIME_RANGE
        and regime.get("strength", 0.0) >= 0.30
    )
