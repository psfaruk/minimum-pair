from typing import Any

from app.candle_shape import recent_fractal_levels

RSI_PERIOD = 14
EMA_PERIOD = 50
BB_PERIOD = 20
BB_STD = 2
SMA_FAST = 8
SMA_SLOW = 21
ATR_PERIOD = 14
FRACTAL_LOOKBACK = 120

# A "squeeze" is RELATIVE, not absolute. The old test — bb_width < 0.5
# — compared a raw (upper-lower)/middle ratio against 0.5, but on FX/OTC
# price scales that ratio lives around 0.0002-0.003, so the condition
# was true on every candle and "squeeze breakout" really meant "any
# close outside the 2-sigma band". A squeeze now means the band is tight
# relative to ITS OWN recent history: current width in the tightest
# quartile of the widths seen over the lookback window.
BB_SQUEEZE_LOOKBACK = 120
BB_SQUEEZE_QUARTILE = 0.25

# S/R levels come from the most recent swing points only — a level from
# two hours ago is not where traders are actually positioned. See
# candle_shape.recent_fractal_levels for the reasoning.
SR_MAX_LEVELS = 6

# NOTE: pyquotex.utils.indicators.TechnicalIndicators rounds every value to
# 2 decimal places internally, which is fine for a 0-100 oscillator like RSI
# but destroys precision for forex/OTC price-scale indicators (EMA, SMA,
# ATR, Bollinger) where a full price move lives in the 4th-5th decimal.
# These are reimplemented here at full precision instead of reusing that
# library's methods.


def _sma(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    out = []
    for i in range(period - 1, len(values)):
        out.append(sum(values[i - period + 1 : i + 1]) / period)
    return out


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float]:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        out.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + rs)))
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = ATR_PERIOD) -> list[float]:
    if len(closes) < period + 1:
        return []
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return _sma(trs, period) if len(trs) >= period else []


def _bollinger(closes: list[float], period: int = BB_PERIOD, num_std: float = BB_STD) -> tuple[float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return mean + num_std * std, mean - num_std * std



def _bb_width_percentile(closes: list[float], current_width: float) -> float | None:
    """Where the current band width sits among the widths of the last
    BB_SQUEEZE_LOOKBACK windows (0 = tightest, 1 = widest)."""
    if current_width is None:
        return None
    end = len(closes)
    start = max(BB_PERIOD, end - BB_SQUEEZE_LOOKBACK)
    widths: list[float] = []
    for j in range(start, end + 1):
        window = closes[j - BB_PERIOD : j]
        if len(window) < BB_PERIOD:
            continue
        mean = sum(window) / BB_PERIOD
        std = (sum((x - mean) ** 2 for x in window) / BB_PERIOD) ** 0.5
        mid = mean if mean else 1e-12
        widths.append(2 * BB_STD * std / mid)
    if len(widths) < 20:  # not enough history to rank against
        return None
    widths.sort()
    below = sum(1 for w in widths if w < current_width)
    return below / len(widths)


def compute(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Computes the indicator snapshot for the most recently closed candle.

    `candles` must be closed candles sorted ascending by time (oldest
    first), each a dict with open/high/low/close/time.
    """
    if len(candles) < SMA_SLOW:
        return {"ready": False}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    close = closes[-1]

    rsi_vals = _rsi(closes, RSI_PERIOD)
    rsi = rsi_vals[-1] if rsi_vals else None

    ema_vals = _ema(closes, EMA_PERIOD)
    ema50 = ema_vals[-1] if ema_vals else None

    atr_vals = _atr(highs, lows, closes, ATR_PERIOD)
    atr14 = atr_vals[-1] if atr_vals else None

    ema_distance_atr = None
    if ema50 is not None and atr14:
        ema_distance_atr = (close - ema50) / atr14

    bb = _bollinger(closes, BB_PERIOD, BB_STD)
    percent_b = None
    bb_upper = bb_middle = bb_lower = bb_width = None
    if bb is not None:
        upper, lower = bb
        bb_upper, bb_lower = upper, lower
        bb_middle = (upper + lower) / 2
        if upper != lower:
            percent_b = (close - lower) / (upper - lower)
            bb_width = (upper - lower) / bb_middle if bb_middle else None

    # Squeeze = current width in the tightest quartile of its own recent
    # history (see the note at BB_SQUEEZE_QUARTILE above).
    width_pctl = _bb_width_percentile(closes, bb_width) if bb_width is not None else None
    bb_squeeze = width_pctl is not None and width_pctl <= BB_SQUEEZE_QUARTILE

    sma_fast_vals = _sma(closes, SMA_FAST)
    sma_slow_vals = _sma(closes, SMA_SLOW)
    sma_cross = None
    fresh_cross = False
    if len(sma_fast_vals) >= 2 and len(sma_slow_vals) >= 2:
        fast_now, fast_prev = sma_fast_vals[-1], sma_fast_vals[-2]
        slow_now, slow_prev = sma_slow_vals[-1], sma_slow_vals[-2]
        sma_cross = "bullish" if fast_now > slow_now else "bearish"
        was_bullish = fast_prev > slow_prev
        fresh_cross = was_bullish != (fast_now > slow_now)

    lookback = candles[-FRACTAL_LOOKBACK:] if len(candles) > FRACTAL_LOOKBACK else candles
    fractal_highs, fractal_lows = recent_fractal_levels(
        [c["high"] for c in lookback], [c["low"] for c in lookback], SR_MAX_LEVELS
    )
    resistance = min((h for h in fractal_highs if h > close), default=None)
    support = max((l for l in fractal_lows if l < close), default=None)

    return {
        "ready": True,
        "close": close,
        "rsi": rsi,
        "ema50": ema50,
        "ema_distance_atr": ema_distance_atr,
        "atr14": atr14,
        "percent_b": percent_b,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "bb_width": bb_width,
        "bb_squeeze": bb_squeeze,
        "bb_width_percentile": width_pctl,
        "sma_cross": sma_cross,
        "sma_fresh_cross": fresh_cross,
        "support": support,
        "resistance": resistance,
    }
