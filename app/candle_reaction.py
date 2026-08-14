"""Candle Reaction detector: did the current candle's wick pierce a known
support/resistance level and get rejected back?

The spec called this the strongest single source and gave it a 0.75 prior
win rate, which decision.py turned into the heaviest vote in the engine.
Backtesting found it nowhere near that. It gets no privileged weight any
more — like every other source it weighs what it has measurably earned
(app/weights.py). It still vetoes signals that fire against it, because
that is a structural argument, not a claim about its hit rate."""
from typing import Any

from app.candle_shape import body, fractal_levels, lower_wick, upper_wick

Candle = dict[str, Any]

PATTERN_NAME = "candle_reaction"

LOOKBACK = 120
MIN_WICK_TO_BODY = 1.5  # wick must dwarf the body for this to count as a rejection, not noise
MIN_WICK_TO_RANGE = 0.4


def detect(candles: list[Candle]) -> tuple[str, str] | None:
    """`candles` are closed candles, oldest first; the last one is what
    gets checked for a rejection wick against levels formed by the
    candles before it."""
    if len(candles) < 10:
        return None

    cur = candles[-1]
    context = candles[-LOOKBACK - 1 : -1] if len(candles) > LOOKBACK + 1 else candles[:-1]
    if len(context) < 5:
        return None

    fractal_highs, fractal_lows = fractal_levels(
        [c["high"] for c in context], [c["low"] for c in context]
    )

    b = max(body(cur), 1e-12)
    up, lo = upper_wick(cur), lower_wick(cur)
    rng = max(cur["high"] - cur["low"], 1e-12)

    for level in fractal_highs:
        if (
            cur["high"] >= level
            and cur["close"] < level
            and up >= MIN_WICK_TO_BODY * b
            and up >= MIN_WICK_TO_RANGE * rng
        ):
            return PATTERN_NAME, "PUT"

    for level in fractal_lows:
        if (
            cur["low"] <= level
            and cur["close"] > level
            and lo >= MIN_WICK_TO_BODY * b
            and lo >= MIN_WICK_TO_RANGE * rng
        ):
            return PATTERN_NAME, "CALL"

    return None
