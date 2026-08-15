"""Candle Reaction detector: did the current candle's wick pierce a known
support/resistance level and get rejected back?

The spec called this the strongest single source and gave it a 0.75 prior
win rate, which decision.py turned into the heaviest vote in the engine.
Backtesting found it nowhere near that. It gets no privileged weight any
more — like every other source it weighs what it has measurably earned
(app/weights.py). It still vetoes signals that fire against it, because
that is a structural argument, not a claim about its hit rate.

# 2026-08 — Reaction family expansion
The web-research audit identified that "candle reaction" should mean
*any* rejection at *any* meaningful level, not only fractal swing S/R.
Practitioner sources (Tradeciety's "7 Rejection Price Patterns",
JumpStartTrading's pin bar guide, FBS, DailyPriceAction) consistently
list four families of rejection levels:

  1. **Fractal swing S/R** (already here) — prior swing highs/lows
  2. **Round numbers** — major price levels like 1.1000, 0.0100 multiples
  3. **Dynamic MAs** — 20-EMA and 50-SMA, which act as dynamic S/R
  4. **Bollinger Band edge** — close outside the band, wick back inside

Each rejection is now classified by which level it rejected, so the
weight layer can learn that (say) "round-number rejection" works on
USD/INR OTC but not on EUR/USD — instead of one opaque "candle_reaction"
bucket mixing them all together.
"""
from typing import Any

from app.candle_shape import body, candle_range, fractal_levels, lower_wick, upper_wick
from app import indicators

Candle = dict[str, Any]

PATTERN_NAME = "candle_reaction"  # legacy, kept for backtest compatibility

LOOKBACK = 120
MIN_WICK_TO_BODY = 1.5  # wick must dwarf the body for this to count as a rejection, not noise
MIN_WICK_TO_RANGE = 0.4

# --- Round-number rejection ------------------------------------------------
# A "round number" is a power-of-10 step in the price. For each pair we
# auto-detect the magnitude from recent history (so 1.10xx forex pairs
# use 0.0010 steps while 5.xx IDR pairs use 0.10 steps) and flag a
# rejection when a wick pierces such a level and closes back inside.
ROUND_NUMBER_REJECT_TOLERANCE = 0.15  # wick must come within this fraction of a step


def _round_number_step(candles: list[Candle]) -> float | None:
    """Auto-detect the natural round-number step for this pair from its
    recent price scale. We pick the smallest power of 10 such that the
    pair's last ~100 candles span at least 5 steps — that's the level
    traders actually watch on this pair."""
    if len(candles) < 10:
        return None
    highs = [c["high"] for c in candles[-100:]]
    span = max(highs) - min(highs)
    if span <= 0:
        return None
    # Powers of 10 from coarse to fine
    for step in (1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001):
        if span / step >= 5:
            return step
    return None


def _nearest_round_number(price: float, step: float) -> float:
    return round(price / step) * step


def _detect_round_number_rejection(cur: Candle, step: float) -> tuple[str, str] | None:
    b = max(body(cur), 1e-12)
    up, lo = upper_wick(cur), lower_wick(cur)
    rng = max(cur["high"] - cur["low"], 1e-12)

    # Upper wick pierces a round number above, closes below it
    upper_level = _nearest_round_number(cur["high"], step)
    if (
        cur["high"] >= upper_level
        and cur["close"] < upper_level
        and up >= MIN_WICK_TO_BODY * b
        and up >= MIN_WICK_TO_RANGE * rng
        and abs(cur["high"] - upper_level) <= ROUND_NUMBER_REJECT_TOLERANCE * step
    ):
        return "round_number_rejection_top", "PUT"

    # Lower wick pierces a round number below, closes above it
    lower_level = _nearest_round_number(cur["low"], step)
    if (
        cur["low"] <= lower_level
        and cur["close"] > lower_level
        and lo >= MIN_WICK_TO_BODY * b
        and lo >= MIN_WICK_TO_RANGE * rng
        and abs(cur["low"] - lower_level) <= ROUND_NUMBER_REJECT_TOLERANCE * step
    ):
        return "round_number_rejection_bottom", "CALL"

    return None


def _detect_ma_rejection(cur: Candle, ind: dict[str, Any] | None) -> tuple[str, str] | None:
    """Wick rejection at the EMA(50) — price came back to the mean,
    wicked through it, closed back on the trend side."""
    if not ind or not ind.get("ready"):
        return None
    ema = ind.get("ema50")
    if ema is None:
        return None
    b = max(body(cur), 1e-12)
    up, lo = upper_wick(cur), lower_wick(cur)
    rng = max(cur["high"] - cur["low"], 1e-12)

    # Price wicked above EMA, closed below → bearish
    if (
        cur["high"] >= ema >= cur["close"]
        and up >= MIN_WICK_TO_BODY * b
        and up >= MIN_WICK_TO_RANGE * rng
    ):
        return "ema_rejection_top", "PUT"
    # Price wicked below EMA, closed above → bullish
    if (
        cur["low"] <= ema <= cur["close"]
        and lo >= MIN_WICK_TO_BODY * b
        and lo >= MIN_WICK_TO_RANGE * rng
    ):
        return "ema_rejection_bottom", "CALL"
    return None


def _detect_bb_rejection(cur: Candle, ind: dict[str, Any] | None) -> tuple[str, str] | None:
    """Bollinger band rejection — close outside band, wick back inside.
    Mean-reversion signal."""
    if not ind or not ind.get("ready"):
        return None
    bb = ind.get("bb_upper"), ind.get("bb_lower")
    if bb is None or bb[0] is None or bb[1] is None:
        return None
    upper, lower = bb
    b = max(body(cur), 1e-12)
    up, lo = upper_wick(cur), lower_wick(cur)

    # Wick pierced upper band, close came back inside → bearish
    if cur["high"] > upper and cur["close"] < upper and up >= MIN_WICK_TO_BODY * b:
        return "bb_rejection_top", "PUT"
    # Wick pierced lower band, close came back inside → bullish
    if cur["low"] < lower and cur["close"] > lower and lo >= MIN_WICK_TO_BODY * b:
        return "bb_rejection_bottom", "CALL"
    return None


def _detect_fractal_rejection(cur: Candle, context: list[Candle]) -> tuple[str, str] | None:
    """Original fractal S/R rejection — kept as the legacy core of the
    candle-reaction family."""
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
            return "fractal_rejection_top", "PUT"

    for level in fractal_lows:
        if (
            cur["low"] <= level
            and cur["close"] > level
            and lo >= MIN_WICK_TO_BODY * b
            and lo >= MIN_WICK_TO_RANGE * rng
        ):
            return "fractal_rejection_bottom", "CALL"

    return None


def detect(candles: list[Candle], ind: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """`candles` are closed candles, oldest first; the last one is what
    gets checked for a rejection wick against levels formed by the
    candles before it.

    `ind` is the indicator snapshot for the same candle (used for
    dynamic-MA and Bollinger rejection). When None, only fractal and
    round-number rejections are checked.
    """
    if len(candles) < 10:
        return None

    cur = candles[-1]
    context = candles[-LOOKBACK - 1 : -1] if len(candles) > LOOKBACK + 1 else candles[:-1]

    # Try fractal first (most specific), then dynamic MA, then Bollinger,
    # then round number. First hit wins — they're not mutually exclusive
    # but reporting multiple rejections of the same candle just fragments
    # the per-source accounting.
    hit = _detect_fractal_rejection(cur, context)
    if hit is None:
        hit = _detect_ma_rejection(cur, ind)
    if hit is None:
        hit = _detect_bb_rejection(cur, ind)
    if hit is None:
        step = _round_number_step(candles)
        if step is not None:
            hit = _detect_round_number_rejection(cur, step)
    return hit
