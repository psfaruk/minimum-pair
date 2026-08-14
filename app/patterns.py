"""Candlestick pattern detectors. Each returns a (name, direction) tuple
or None. `PRIOR_WIN_RATE` gives each pattern a starting structural prior —
used as the confidence input until enough measured history (>=5 graded
signals for that pattern+pair, see decision.py) takes over."""
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

PRIOR_WIN_RATE: dict[str, float] = {
    "doji_reversal": 0.52,
    "tweezer_top": 0.55,
    "tweezer_bottom": 0.55,
    "harami_bull": 0.53,
    "harami_bear": 0.53,
    "wickwall": 0.58,
    "marubozu": 0.56,
    "hammer": 0.54,
    "shooting_star": 0.54,
    "engulfing_bull": 0.57,
    "engulfing_bear": 0.57,
}


def _doji_reversal(candles: list[Candle]) -> tuple[str, str] | None:
    c = candles[-1]
    if body_ratio(c) > 0.1:
        return None
    trend = trend_direction(candles[:-1])
    if trend == "up":
        return "doji_reversal", "PUT"
    if trend == "down":
        return "doji_reversal", "CALL"
    return None


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
    if is_bear(prev) and is_bull(cur):
        return "harami_bull", "CALL"
    if is_bull(prev) and is_bear(cur):
        return "harami_bear", "PUT"
    return None


def _wickwall(candles: list[Candle]) -> tuple[str, str] | None:
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    if up >= 3 * b and up >= 0.5 * candle_range(c):
        return "wickwall", "PUT"
    if lo >= 3 * b and lo >= 0.5 * candle_range(c):
        return "wickwall", "CALL"
    return None


def _marubozu(candles: list[Candle]) -> tuple[str, str] | None:
    c = candles[-1]
    r = candle_range(c)
    if upper_wick(c) > 0.05 * r or lower_wick(c) > 0.05 * r:
        return None
    if is_bull(c):
        return "marubozu", "CALL"
    if is_bear(c):
        return "marubozu", "PUT"
    return None


def _hammer_shooting_star(candles: list[Candle]) -> tuple[str, str] | None:
    c = candles[-1]
    b = max(body(c), 1e-12)
    up, lo = upper_wick(c), lower_wick(c)
    trend = trend_direction(candles[:-1])
    if lo >= 2 * b and up <= 0.3 * b and trend == "down":
        return "hammer", "CALL"
    if up >= 2 * b and lo <= 0.3 * b and trend == "up":
        return "shooting_star", "PUT"
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


_DETECTORS = [
    _doji_reversal,
    _tweezer,
    _harami,
    _wickwall,
    _marubozu,
    _hammer_shooting_star,
    _engulfing,
]


def detect(candles: list[Candle]) -> list[tuple[str, str, float]]:
    """Runs every detector against the most recent candle(s). Returns a
    list of (pattern_name, direction, prior_win_rate)."""
    if len(candles) < 3:
        return []
    hits = []
    for detector in _DETECTORS:
        result = detector(candles)
        if result is not None:
            name, direction = result
            hits.append((name, direction, PRIOR_WIN_RATE[name]))
    return hits
