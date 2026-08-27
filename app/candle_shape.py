"""Shared candle-geometry helpers used by patterns.py, candle_reaction.py
and microstructure.py — kept in one place so "body", "wick", "trend" mean
the same thing everywhere."""
from typing import Any

Candle = dict[str, Any]


def body(c: Candle) -> float:
    return abs(c["close"] - c["open"])


def candle_range(c: Candle) -> float:
    return max(c["high"] - c["low"], 1e-12)


def upper_wick(c: Candle) -> float:
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c: Candle) -> float:
    return min(c["open"], c["close"]) - c["low"]


def is_bull(c: Candle) -> bool:
    return c["close"] > c["open"]


def is_bear(c: Candle) -> bool:
    return c["close"] < c["open"]


def body_ratio(c: Candle) -> float:
    return body(c) / candle_range(c)


def fractal_levels(highs: list[float], lows: list[float]) -> tuple[list[float], list[float]]:
    """Classic 2-bar-either-side fractal swing highs/lows — used both for
    the indicator engine's S/R distance check and for the candle-reaction
    (wick rejection) detector."""
    fractal_highs, fractal_lows = [], []
    n = len(highs)
    for i in range(2, n - 2):
        window_h = highs[i - 2 : i + 3]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            fractal_highs.append(highs[i])
        window_l = lows[i - 2 : i + 3]
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            fractal_lows.append(lows[i])
    return fractal_highs, fractal_lows


def recent_fractal_levels(
    highs: list[float], lows: list[float], max_levels: int = 6
) -> tuple[list[float], list[float]]:
    """Only the most recent `max_levels` swing highs and swing lows.

    The old callers fed fractal_levels() the raw 120-candle lookback,
    which returns hundreds of levels — a wick touches *some* level on
    nearly every candle, so the rejection detector fired constantly and
    the S/R distance votes were computed against prices formed up to two
    hours earlier. Traders watch a handful of the most recent swings;
    this returns exactly that (most recent first)."""
    fractal_highs, fractal_lows = fractal_levels(highs, lows)
    return (
        list(reversed(fractal_highs[-max_levels:])),
        list(reversed(fractal_lows[-max_levels:])),
    )


def trend_direction(candles: list[Candle], lookback: int = 5) -> str:
    """Crude short trend read: net close-to-close drift over the last
    `lookback` candles. Used to give reversal patterns context (a hammer
    only means something if it follows a downtrend)."""
    window = candles[-lookback:]
    if len(window) < 2:
        return "flat"
    drift = window[-1]["close"] - window[0]["close"]
    avg_range = sum(candle_range(c) for c in window) / len(window)
    if avg_range <= 0:
        return "flat"
    if drift > 0.3 * avg_range:
        return "up"
    if drift < -0.3 * avg_range:
        return "down"
    return "flat"
