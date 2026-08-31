"""Higher-timeframe (HTF) context — the "zoom out before you zoom in"
layer.

The decision engine reads only 1-minute candles. On its own that window
is dominated by per-candle noise: RSI can be "oversold" all the way down
a 5-minute trend, and a floor bounce inside a falling 5-minute leg is a
lot more likely to fail than one that agrees with the higher leg. Real
traders check the higher timeframe first; the engine now does too.

The context is deliberately cheap and self-contained: it aggregates the
closed 1-minute candle list into HTF buckets, then reads:

  1. **Position vs the HTF EMA** — above/below the higher-timeframe mean;
  2. **EMA slope** — is that mean rising or falling;
  3. **Net displacement of the last few HTF buckets** against the average
     bucket range — is the higher leg actually travelling.

All three are combined into a single `trend` ("up" | "down" | "flat")
and a 0..1 `strength`, so the decision engine can add one honest
higher-timeframe vote and the fallback path can use it as a better-than-
colour baseline. Everything degrades gracefully to "flat" when there is
not enough history — short bootstrap windows must never invent a trend.
"""
from typing import Any

Candle = dict[str, Any]

HTF_MINUTES = 5          # 5-minute context over the 1-minute feed
EMA_PERIOD = 20          # HTF EMA period, in HTF buckets
NET_BUCKETS = 3          # net displacement window, in HTF buckets
MIN_BUCKETS = 12         # fewer than this → flat (no honest read)
LOOKBACK_BUCKETS = 40    # aggregate at most this many recent buckets

# How far the close must sit from the HTF EMA (in bucket ranges) and how
# steep the EMA must be before "position" and "slope" earn a say.
POS_SCALE = 1.2
SLOPE_MIN = 0.05


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def htf_context(candles: list[Candle], period_seconds: int = 60) -> dict[str, Any]:
    """Reads the 5-minute trend off the closed 1-minute candle list.

    Accepts the same cleaned (synthetic-stripped), ascending, closed
    candle list the rest of the engine uses. Never raises on short
    input — returns a flat read instead.
    """
    empty = {"trend": "flat", "strength": 0.0, "buckets": 0, "ema": None}
    if not candles:
        return empty

    bucket_seconds = HTF_MINUTES * period_seconds
    has_ts = all("ts" in c for c in candles)
    closes: dict[int, float] = {}
    for idx, c in enumerate(candles):
        if c.get("synthetic"):
            continue
        # Candles without a `ts` (synthetic test harnesses, replay tools)
        # are bucketed positionally — they are by construction an
        # unbroken 1-minute sequence.
        bucket = (int(c["ts"]) // bucket_seconds) if has_ts else idx // HTF_MINUTES
        closes[bucket] = c["close"]
    if len(closes) < MIN_BUCKETS:
        return empty

    ordered = [closes[key] for key in sorted(closes)[-LOOKBACK_BUCKETS:]]
    n = len(ordered)

    ema_vals = _ema(ordered, EMA_PERIOD)
    if not ema_vals:
        return empty
    ema = ema_vals[-1]
    close = ordered[-1]

    ranges = [
        abs(ordered[i] - ordered[i - 1])
        for i in range(max(1, n - NET_BUCKETS), n)
    ]
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0
    if avg_range <= 0:
        return empty

    # Position: how many bucket-ranges the close sits above/below the EMA.
    pos = (close - ema) / (avg_range * POS_SCALE) if avg_range else 0.0
    # Slope: the EMA's own last step, normalized the same way.
    slope = (ema_vals[-1] - ema_vals[-2]) / (avg_range * POS_SCALE) if len(ema_vals) >= 2 else 0.0
    # Net displacement of the last few buckets, normalized.
    net = (ordered[-1] - ordered[-1 - NET_BUCKETS]) / (avg_range * NET_BUCKETS) if n > NET_BUCKETS else 0.0

    score = 0.40 * max(-1.0, min(1.0, pos)) + 0.25 * max(-1.0, min(1.0, slope)) + 0.35 * max(-1.0, min(1.0, net))

    if score > 0.18:
        trend = "up"
    elif score < -0.18:
        trend = "down"
    else:
        trend = "flat"

    return {
        "trend": trend,
        "strength": round(min(abs(score), 1.0), 3),
        "buckets": n,
        "ema": ema,
    }
