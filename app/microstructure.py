"""Hand-weighted microstructure scorer: blends candle color, body
strength, wick imbalance, short trend, and same-color streak into a
single directional score. Used two ways in decision.py:
  1. as the ALWAYS_SIGNAL fallback source when nothing else fires
  2. as an agreement check — a pattern/indicator signal against the
     microstructure read is treated as weaker evidence, not vetoed
     outright (microstructure is a rough, noisy read on its own)."""
from typing import Any

from app.candle_shape import body_ratio, candle_range, is_bull, lower_wick, trend_direction, upper_wick

Candle = dict[str, Any]

PATTERN_NAME = "microstructure"

W_COLOR = 0.25
W_BODY = 0.25
W_WICK = 0.25
W_TREND = 0.15
W_STREAK = 0.10

STREAK_CAP = 4


def _streak(candles: list[Candle]) -> tuple[int, str]:
    if not candles:
        return 0, "flat"
    last_dir = "up" if is_bull(candles[-1]) else "down"
    n = 0
    for c in reversed(candles):
        d = "up" if is_bull(c) else "down"
        if d != last_dir:
            break
        n += 1
    return min(n, STREAK_CAP), last_dir


def score(candles: list[Candle]) -> dict[str, Any]:
    """Returns {"direction": "CALL"|"PUT"|None, "strength": 0..1}."""
    if not candles:
        return {"direction": None, "strength": 0.0}

    cur = candles[-1]
    color = 1.0 if is_bull(cur) else -1.0
    body_component = color * body_ratio(cur)

    rng = candle_range(cur)
    wick_component = (lower_wick(cur) - upper_wick(cur)) / rng

    trend = trend_direction(candles[:-1] or candles)
    trend_component = {"up": 1.0, "down": -1.0, "flat": 0.0}[trend]

    streak_len, streak_dir = _streak(candles)
    streak_component = (streak_len / STREAK_CAP) * (1.0 if streak_dir == "up" else -1.0)

    raw = (
        W_COLOR * color
        + W_BODY * body_component
        + W_WICK * wick_component
        + W_TREND * trend_component
        + W_STREAK * streak_component
    )
    strength = min(abs(raw), 1.0)
    if strength < 0.05:
        return {"direction": None, "strength": strength}
    return {"direction": "CALL" if raw > 0 else "PUT", "strength": strength}
