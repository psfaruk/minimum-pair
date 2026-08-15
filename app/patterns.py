"""Candlestick pattern detectors. Each returns a (name, direction) tuple
or None.

These used to carry a PRIOR_WIN_RATE table — 0.52 for a doji, 0.57 for an
engulfing, and so on — which decision.py turned directly into vote
weight. Those numbers were folklore, never measured, and a backtest put
several of them on the wrong side of 50%. A pattern's weight now comes
from its own measured record on the pair it fired on (app/weights.py), so
there is nothing left for a prior to do.

# 2026-08 — Pattern expansion
The original set covered 7 patterns. The web-research audit
(/home/z/my-project/download/research/binary-trading-research.md)
identified that Investopedia/StockCharts consider Engulfing + the
3-candle "star" family + Three Soldiers/Crows as the most reliable
reversal signals, and that the previous set was conflating several
shapes (Hammer vs Hanging Man; Inverted Hammer vs Shooting Star;
Dragonfly vs Gravestone Doji). Each of those conflations silently
produced wrong-direction calls on the pairs where the conflated context
was the wrong one. This file now distinguishes every shape, requires the
correct trend context, and adds the missing high-value 2-candle and
3-candle patterns.
"""
from typing import Any

from app.candle_shape import (
    body,
    body_ratio,
    candle_range,
    is_bear,
    is_bull,
    lower_wick,
    trend_direction,
    upper_wick,
)

Candle = dict[str, Any]


# ---------------------------------------------------------------------------
# Single-candle patterns
# ---------------------------------------------------------------------------

def _doji(c: Candle) -> str | None:
    """Classify a doji by wick shape. Returns 'classic' | 'dragonfly' |
    'gravestone' | 'long_legged' | None. Direction is then decided by the
    caller using the prior trend context — a Dragonfly in an uptrend is a
    bearish reversal, the same shape in a downtrend is bullish."""
    if body_ratio(c) > 0.1:
        return None
    up, lo = upper_wick(c), lower_wick(c)
    r = candle_range(c)
    if r <= 0:
        return "classic"
    up_p, lo_p = up / r, lo / r
    # Dragonfly: open=close=high (no upper wick), long lower wick
    if up_p < 0.05 and lo_p > 0.6:
        return "dragonfly"
    # Gravestone: open=close=low (no lower wick), long upper wick
    if lo_p < 0.05 and up_p > 0.6:
        return "gravestone"
    # Long-legged: both wicks substantial
    if up_p > 0.3 and lo_p > 0.3:
        return "long_legged"
    return "classic"


def _doji_reversal(candles: list[Candle]) -> tuple[str, str] | None:
    c = candles[-1]
    kind = _doji(c)
    if kind is None:
        return None
    trend = trend_direction(candles[:-1])
    # A doji in a flat market is just indecision — no directional edge.
    if trend == "flat":
        return None
    if kind == "dragonfly":
        # Dragonfly after a downtrend → bullish reversal
        # Dragonfly after an uptrend → bearish reversal (failed new high)
        return "doji_dragonfly", "CALL" if trend == "down" else "PUT"
    if kind == "gravestone":
        # Gravestone after an uptrend → bearish reversal
        # Gravestone after a downtrend → bullish reversal (failed new low)
        return "doji_gravestone", "PUT" if trend == "up" else "CALL"
    if kind == "long_legged":
        # High-volatility indecision — bias against the prior trend
        return "doji_long_legged", "PUT" if trend == "up" else "CALL"
    # Classic doji — weak reversal signal, only against a strong trend
    return "doji_reversal", "PUT" if trend == "up" else "CALL"


def _marubozu(candles: list[Candle]) -> tuple[str, str] | None:
    """Marubozu = no wicks (or vanishingly small). Strong conviction in
    the candle's direction → continuation, not reversal."""
    c = candles[-1]
    r = candle_range(c)
    if upper_wick(c) > 0.05 * r or lower_wick(c) > 0.05 * r:
        return None
    if body_ratio(c) < 0.9:
        return None
    if is_bull(c):
        return "marubozu_bull", "CALL"
    if is_bear(c):
        return "marubozu_bear", "PUT"
    return None


def _hammer(candles: list[Candle]) -> tuple[str, str] | None:
    """Hammer: small body at top, long lower wick (≥2× body), in a
    downtrend → bullish reversal. Same shape in an uptrend is a Hanging
    Man (bearish) — see _hanging_man."""
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if lo < 2 * b or up > 0.3 * b:
        return None
    if body_ratio(c) > 0.3:
        return None
    if trend_direction(candles[:-1]) != "down":
        return None
    return "hammer", "CALL"


def _hanging_man(candles: list[Candle]) -> tuple[str, str] | None:
    """Hanging Man: same shape as Hammer but appears in an uptrend →
    bearish reversal. Previously conflated with Hammer, which produced
    wrong-direction calls whenever the shape appeared after an up-move."""
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if lo < 2 * b or up > 0.3 * b:
        return None
    if body_ratio(c) > 0.3:
        return None
    if trend_direction(candles[:-1]) != "up":
        return None
    return "hanging_man", "PUT"


def _shooting_star(candles: list[Candle]) -> tuple[str, str] | None:
    """Shooting Star: small body at bottom, long upper wick (≥2× body),
    in an uptrend → bearish reversal. Same shape in a downtrend is an
    Inverted Hammer (bullish) — see _inverted_hammer."""
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if up < 2 * b or lo > 0.3 * b:
        return None
    if body_ratio(c) > 0.3:
        return None
    if trend_direction(candles[:-1]) != "up":
        return None
    return "shooting_star", "PUT"


def _inverted_hammer(candles: list[Candle]) -> tuple[str, str] | None:
    """Inverted Hammer: same shape as Shooting Star but appears in a
    downtrend → bullish reversal (failed new low)."""
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if up < 2 * b or lo > 0.3 * b:
        return None
    if body_ratio(c) > 0.3:
        return None
    if trend_direction(candles[:-1]) != "down":
        return None
    return "inverted_hammer", "CALL"


def _spinning_top(candles: list[Candle]) -> tuple[str, str] | None:
    """Spinning top: small body, long wicks both sides. Pure indecision
    — only fires as a weak counter-trend signal when the prior trend was
    strong enough that indecision itself is information."""
    c = candles[-1]
    if body_ratio(c) > 0.3:
        return None
    up, lo = upper_wick(c), lower_wick(c)
    r = candle_range(c)
    if up < 0.3 * r or lo < 0.3 * r:
        return None
    trend = trend_direction(candles[:-1])
    if trend == "flat":
        return None
    return "spinning_top", "PUT" if trend == "up" else "CALL"


def _wickwall(candles: list[Candle]) -> tuple[str, str] | None:
    """Wickwall: one wick dwarfs the body and the other wick (≥3× body,
    ≥0.5 of range). The body's side is the direction price tried to push
    and failed; signal is opposite of the long wick."""
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if up >= 3 * b and up >= 0.5 * candle_range(c):
        return "wickwall_top", "PUT"
    if lo >= 3 * b and lo >= 0.5 * candle_range(c):
        return "wickwall_bottom", "CALL"
    return None


# ---------------------------------------------------------------------------
# Two-candle patterns
# ---------------------------------------------------------------------------

def _tweezer(candles: list[Candle]) -> tuple[str, str] | None:
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    tol = 0.1 * candle_range(prev)
    if abs(prev["high"] - cur["high"]) <= tol and is_bull(prev) and is_bear(cur):
        return "tweezer_top", "PUT"
    if abs(prev["low"] - cur["low"]) <= tol and is_bear(prev) and is_bull(cur):
        return "tweezer_bottom", "CALL"
    return None


def _harami(candles: list[Candle]) -> tuple[str, str] | None:
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    prev_hi, prev_lo = max(prev["open"], prev["close"]), min(prev["open"], prev["close"])
    cur_hi, cur_lo = max(cur["open"], cur["close"]), min(cur["open"], cur["close"])
    if body(prev) <= 0 or cur_hi >= prev_hi or cur_lo <= prev_lo:
        return None
    # Prior body should be substantial (not itself a doji)
    if body_ratio(prev) < 0.5:
        return None
    if is_bear(prev) and is_bull(cur):
        return "harami_bull", "CALL"
    if is_bull(prev) and is_bear(cur):
        return "harami_bear", "PUT"
    return None


def _harami_cross(candles: list[Candle]) -> tuple[str, str] | None:
    """Harami Cross: doji inside the prior large body. Stronger reversal
    warning than the basic harami because the doji shows the prior move
    has fully stalled."""
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if body_ratio(cur) > 0.1:
        return None
    prev_hi, prev_lo = max(prev["open"], prev["close"]), min(prev["open"], prev["close"])
    cur_hi, cur_lo = max(cur["open"], cur["close"]), min(cur["open"], cur["close"])
    if body(prev) <= 0 or cur_hi >= prev_hi or cur_lo <= prev_lo:
        return None
    if body_ratio(prev) < 0.5:
        return None
    if is_bear(prev):
        return "harami_cross_bull", "CALL"
    if is_bull(prev):
        return "harami_cross_bear", "PUT"
    return None


def _engulfing(candles: list[Candle]) -> tuple[str, str] | None:
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if body(prev) <= 0 or body(cur) <= body(prev):
        return None
    if is_bear(prev) and is_bull(cur) and cur["open"] <= prev["close"] and cur["close"] >= prev["open"]:
        return "engulfing_bull", "CALL"
    if is_bull(prev) and is_bear(cur) and cur["open"] >= prev["close"] and cur["close"] <= prev["open"]:
        return "engulfing_bear", "PUT"
    return None


def _piercing_line(candles: list[Candle]) -> tuple[str, str] | None:
    """Piercing Line: prior bearish candle, current bullish candle that
    opens below the prior low (or close) and closes above the prior
    midpoint but below the prior open. Bullish reversal."""
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if not (is_bear(prev) and is_bull(cur)):
        return None
    prev_mid = (prev["open"] + prev["close"]) / 2
    # Current opens below prior close (gap down) then closes above midpoint
    if cur["open"] >= prev["close"]:
        return None
    if cur["close"] <= prev_mid or cur["close"] >= prev["open"]:
        return None
    return "piercing_line", "CALL"


def _dark_cloud_cover(candles: list[Candle]) -> tuple[str, str] | None:
    """Dark Cloud Cover: prior bullish candle, current bearish candle
    that opens above the prior high (gap up) and closes below the prior
    midpoint but above the prior close. Bearish reversal."""
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if not (is_bull(prev) and is_bear(cur)):
        return None
    prev_mid = (prev["open"] + prev["close"]) / 2
    if cur["open"] <= prev["close"]:
        return None
    if cur["close"] >= prev_mid or cur["close"] <= prev["open"]:
        return None
    return "dark_cloud_cover", "PUT"


# ---------------------------------------------------------------------------
# Three-candle patterns
# ---------------------------------------------------------------------------

def _morning_star(candles: list[Candle]) -> tuple[str, str] | None:
    """Morning Star: long bearish → small body/doji (gap down) → long
    bullish closing above the midpoint of the first candle. Bullish
    reversal at the end of a downtrend."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bear(first):
        return None
    if body_ratio(first) < 0.5:
        return None
    # Second candle: small body (doji or spinning top)
    if body_ratio(second) > 0.3:
        return None
    # Third candle: bullish, closes above midpoint of first
    if not is_bull(third):
        return None
    if body_ratio(third) < 0.5:
        return None
    first_mid = (first["open"] + first["close"]) / 2
    if third["close"] <= first_mid:
        return None
    return "morning_star", "CALL"


def _evening_star(candles: list[Candle]) -> tuple[str, str] | None:
    """Evening Star: long bullish → small body/doji (gap up) → long
    bearish closing below the midpoint of the first candle. Bearish
    reversal at the end of an uptrend."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bull(first):
        return None
    if body_ratio(first) < 0.5:
        return None
    if body_ratio(second) > 0.3:
        return None
    if not is_bear(third):
        return None
    if body_ratio(third) < 0.5:
        return None
    first_mid = (first["open"] + first["close"]) / 2
    if third["close"] >= first_mid:
        return None
    return "evening_star", "PUT"


def _three_white_soldiers(candles: list[Candle]) -> tuple[str, str] | None:
    """Three White Soldiers: three bullish candles, each opening inside
    the prior body and closing higher. Strong bullish reversal after a
    downtrend. Per Investopedia, this is one of the most reliable
    reversal signals."""
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    if not (is_bull(a) and is_bull(b) and is_bull(c)):
        return None
    # Use 0.45 (not 0.5) so floating-point edge cases like 0.499999...
    # don't silently fail what is clearly a substantial body.
    if body_ratio(a) < 0.45 or body_ratio(b) < 0.45 or body_ratio(c) < 0.45:
        return None
    # Each opens within prior body and closes higher
    if b["open"] < min(a["open"], a["close"]) or b["open"] > max(a["open"], a["close"]):
        return None
    if c["open"] < min(b["open"], b["close"]) or c["open"] > max(b["open"], b["close"]):
        return None
    if c["close"] <= b["close"] or b["close"] <= a["close"]:
        return None
    # Should appear after a downtrend (otherwise it's just continuation)
    if trend_direction(candles[:-3]) != "down":
        return None
    return "three_white_soldiers", "CALL"


def _three_black_crows(candles: list[Candle]) -> tuple[str, str] | None:
    """Three Black Crows: mirror of Three White Soldiers. Three bearish
    candles, each opening inside prior body and closing lower, after an
    uptrend."""
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    if not (is_bear(a) and is_bear(b) and is_bear(c)):
        return None
    if body_ratio(a) < 0.45 or body_ratio(b) < 0.45 or body_ratio(c) < 0.45:
        return None
    if b["open"] < min(a["open"], a["close"]) or b["open"] > max(a["open"], a["close"]):
        return None
    if c["open"] < min(b["open"], b["close"]) or c["open"] > max(b["open"], b["close"]):
        return None
    if c["close"] >= b["close"] or b["close"] >= a["close"]:
        return None
    if trend_direction(candles[:-3]) != "up":
        return None
    return "three_black_crows", "PUT"


def _three_inside_up(candles: list[Candle]) -> tuple[str, str] | None:
    """Three Inside Up: harami + bullish confirmation candle. Stronger
    than harami alone."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bear(first) or not is_bull(second) or not is_bull(third):
        return None
    # Second is inside first
    if second["open"] < min(first["open"], first["close"]) or second["close"] > max(first["open"], first["close"]):
        return None
    # Third closes above first's open
    if third["close"] <= first["open"]:
        return None
    return "three_inside_up", "CALL"


def _three_inside_down(candles: list[Candle]) -> tuple[str, str] | None:
    """Three Inside Down: harami + bearish confirmation."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bull(first) or not is_bear(second) or not is_bear(third):
        return None
    if second["open"] < min(first["open"], first["close"]) or second["close"] > max(first["open"], first["close"]):
        return None
    if third["close"] >= first["open"]:
        return None
    return "three_inside_down", "PUT"


def _three_outside_up(candles: list[Candle]) -> tuple[str, str] | None:
    """Three Outside Up: bullish engulfing + bullish confirmation."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bear(first) or not is_bull(second):
        return None
    if body(second) <= body(first):
        return None
    if second["open"] > first["close"] or second["close"] < first["open"]:
        return None
    if not is_bull(third) or third["close"] <= second["close"]:
        return None
    return "three_outside_up", "CALL"


def _three_outside_down(candles: list[Candle]) -> tuple[str, str] | None:
    """Three Outside Down: bearish engulfing + bearish confirmation."""
    if len(candles) < 3:
        return None
    first, second, third = candles[-3], candles[-2], candles[-1]
    if not is_bull(first) or not is_bear(second):
        return None
    if body(second) <= body(first):
        return None
    if second["open"] < first["close"] or second["close"] > first["open"]:
        return None
    if not is_bear(third) or third["close"] >= second["close"]:
        return None
    return "three_outside_down", "PUT"


_DETECTORS = [
    # Single-candle
    _doji_reversal,
    _marubozu,
    _hammer,
    _hanging_man,
    _shooting_star,
    _inverted_hammer,
    _spinning_top,
    _wickwall,
    # Two-candle
    _tweezer,
    _harami,
    _harami_cross,
    _engulfing,
    _piercing_line,
    _dark_cloud_cover,
    # Three-candle
    _morning_star,
    _evening_star,
    _three_white_soldiers,
    _three_black_crows,
    _three_inside_up,
    _three_inside_down,
    _three_outside_up,
    _three_outside_down,
]


def detect(candles: list[Candle]) -> list[tuple[str, str]]:
    """Runs every detector against the most recent candle(s). Returns a
    list of (pattern_name, direction)."""
    if len(candles) < 3:
        return []
    return [result for detector in _DETECTORS if (result := detector(candles)) is not None]


# A registry exposed for /api/strategies and for the docs. Each entry is
# (name, family, candles_required, typical_direction, source) — useful
# for the open API so consumers know which strategies exist and what
# they mean.
PATTERN_REGISTRY: list[dict[str, str]] = [
    {"name": "doji_reversal",        "family": "single", "candles": 1, "bias": "reversal",  "description": "Classic doji against a strong prior trend"},
    {"name": "doji_dragonfly",       "family": "single", "candles": 1, "bias": "reversal",  "description": "Dragonfly doji — open=close=high, long lower wick"},
    {"name": "doji_gravestone",      "family": "single", "candles": 1, "bias": "reversal",  "description": "Gravestone doji — open=close=low, long upper wick"},
    {"name": "doji_long_legged",     "family": "single", "candles": 1, "bias": "reversal",  "description": "Long-legged doji — high-volatility indecision"},
    {"name": "marubozu_bull",        "family": "single", "candles": 1, "bias": "continuation","description": "Bullish marubozu — no wicks, strong buying"},
    {"name": "marubozu_bear",        "family": "single", "candles": 1, "bias": "continuation","description": "Bearish marubozu — no wicks, strong selling"},
    {"name": "hammer",               "family": "single", "candles": 1, "bias": "bullish_rev","description": "Hammer in a downtrend → bullish reversal"},
    {"name": "hanging_man",          "family": "single", "candles": 1, "bias": "bearish_rev","description": "Hanging Man in an uptrend → bearish reversal"},
    {"name": "shooting_star",        "family": "single", "candles": 1, "bias": "bearish_rev","description": "Shooting Star in an uptrend → bearish reversal"},
    {"name": "inverted_hammer",      "family": "single", "candles": 1, "bias": "bullish_rev","description": "Inverted Hammer in a downtrend → bullish reversal"},
    {"name": "spinning_top",         "family": "single", "candles": 1, "bias": "reversal",  "description": "Spinning top — small body, both wicks long"},
    {"name": "wickwall_top",         "family": "single", "candles": 1, "bias": "bearish_rev","description": "Upper wick dwarfs body — sellers rejected"},
    {"name": "wickwall_bottom",      "family": "single", "candles": 1, "bias": "bullish_rev","description": "Lower wick dwarfs body — buyers rejected"},
    {"name": "tweezer_top",          "family": "double", "candles": 2, "bias": "bearish_rev","description": "Two candles with matching highs after an uptrend"},
    {"name": "tweezer_bottom",       "family": "double", "candles": 2, "bias": "bullish_rev","description": "Two candles with matching lows after a downtrend"},
    {"name": "harami_bull",          "family": "double", "candles": 2, "bias": "bullish_rev","description": "Small bullish candle inside prior bearish body"},
    {"name": "harami_bear",          "family": "double", "candles": 2, "bias": "bearish_rev","description": "Small bearish candle inside prior bullish body"},
    {"name": "harami_cross_bull",    "family": "double", "candles": 2, "bias": "bullish_rev","description": "Doji inside prior bearish body — stronger than harami"},
    {"name": "harami_cross_bear",    "family": "double", "candles": 2, "bias": "bearish_rev","description": "Doji inside prior bullish body — stronger than harami"},
    {"name": "engulfing_bull",       "family": "double", "candles": 2, "bias": "bullish_rev","description": "Bullish candle fully engulfs prior bearish body"},
    {"name": "engulfing_bear",       "family": "double", "candles": 2, "bias": "bearish_rev","description": "Bearish candle fully engulfs prior bullish body"},
    {"name": "piercing_line",        "family": "double", "candles": 2, "bias": "bullish_rev","description": "Bullish candle gaps down, closes above prior midpoint"},
    {"name": "dark_cloud_cover",     "family": "double", "candles": 2, "bias": "bearish_rev","description": "Bearish candle gaps up, closes below prior midpoint"},
    {"name": "morning_star",         "family": "triple", "candles": 3, "bias": "bullish_rev","description": "Bearish → doji → bullish closing above first midpoint"},
    {"name": "evening_star",         "family": "triple", "candles": 3, "bias": "bearish_rev","description": "Bullish → doji → bearish closing below first midpoint"},
    {"name": "three_white_soldiers", "family": "triple", "candles": 3, "bias": "bullish_rev","description": "Three bullish candles, each opening inside prior body"},
    {"name": "three_black_crows",    "family": "triple", "candles": 3, "bias": "bearish_rev","description": "Three bearish candles, each opening inside prior body"},
    {"name": "three_inside_up",      "family": "triple", "candles": 3, "bias": "bullish_rev","description": "Harami + bullish confirmation candle"},
    {"name": "three_inside_down",    "family": "triple", "candles": 3, "bias": "bearish_rev","description": "Harami + bearish confirmation candle"},
    {"name": "three_outside_up",     "family": "triple", "candles": 3, "bias": "bullish_rev","description": "Bullish engulfing + bullish confirmation candle"},
    {"name": "three_outside_down",   "family": "triple", "candles": 3, "bias": "bearish_rev","description": "Bearish engulfing + bearish confirmation candle"},
]
