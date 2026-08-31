"""Measures raw next-candle edges of simple reads on the synthetic
generators — the ground truth the fallback path should be built on.

For each generator x seed, scores:
  color_follow     next candle continues last candle's colour
  color_fade       next candle reverses last candle's colour
  streak3_fade     after >=3 same-colour candles, fade the streak
  streak2_fade     after >=2 same-colour candles, fade the streak
  body_fade        fade a full-bodied candle (|body| >= 0.6 * range)
  wick_fade_call   long lower wick -> CALL / long upper wick -> PUT
  anchor_fade      fade displacement from the 60-candle mean (in ATRs)
  micro_fade_0.15  microstructure.score(fade=True) at strength >= 0.15
"""
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest_engine import GENERATORS  # noqa: E402
from app import candle_shape, microstructure  # noqa: E402

SEEDS = [7, 11, 23, 42, 99]
BARS = 2000
WARMUP = 60


def streak_len(candles):
    last = candles[-1]
    if last["close"] == last["open"]:
        return 0, None
    up = last["close"] > last["open"]
    n = 0
    for c in reversed(candles):
        if c["close"] == c["open"] or (c["close"] > c["open"]) != up:
            break
        n += 1
    return n, ("CALL" if up else "PUT")


def measure(gen_name):
    rates = {}
    for seed in SEEDS:
        candles = GENERATORS[gen_name](BARS, seed)
        tally = {k: [0, 0] for k in
                 ("color_follow", "color_fade", "streak3_fade", "streak2_fade",
                  "body_fade", "wick_fade", "anchor_fade", "micro_fade")}
        for i in range(WARMUP, len(candles) - 1):
            hist = candles[: i + 1]
            cur, nxt = candles[i], candles[i + 1]
            if nxt["close"] == cur["close"]:
                continue
            outcome_call = nxt["close"] > cur["close"]

            def grade(key, says_call):
                if says_call is None:
                    return
                tally[key][0 if says_call == ("CALL" if outcome_call else "PUT") else 1] += 1

            n, sdir = streak_len(hist)
            grade("color_follow", sdir if n >= 1 else None)
            grade("color_fade", {"CALL": "PUT", "PUT": "CALL"}.get(sdir) if n >= 1 else None)
            grade("streak3_fade", {"CALL": "PUT", "PUT": "CALL"}.get(sdir) if n >= 3 else None)
            grade("streak2_fade", {"CALL": "PUT", "PUT": "CALL"}.get(sdir) if n >= 2 else None)

            body = candle_shape.body(cur) / candle_shape.range_of(cur) if hasattr(candle_shape, "range_of") else None
            rng = max(cur["high"] - cur["low"], 1e-12)
            br = abs(cur["close"] - cur["open"]) / rng
            grade("body_fade", ("PUT" if cur["close"] > cur["open"] else "CALL") if br >= 0.6 else None)

            lw = (min(cur["open"], cur["close"]) - cur["low"]) / rng
            uw = (cur["high"] - max(cur["open"], cur["close"])) / rng
            if lw >= 0.4 and lw > 2 * uw:
                grade("wick_fade", "CALL")
            elif uw >= 0.4 and uw > 2 * lw:
                grade("wick_fade", "PUT")

            closes = [c["close"] for c in hist[-60:]]
            mean = sum(closes) / len(closes)
            atr = sum(max(c["high"] - c["low"], 1e-12) for c in hist[-14:]) / 14
            disp = (cur["close"] - mean) / atr
            if disp >= 1.0:
                grade("anchor_fade", "PUT")
            elif disp <= -1.0:
                grade("anchor_fade", "CALL")

            m = microstructure.score(hist, fade=True)
            if m["direction"] is not None and m["strength"] >= 0.15:
                grade("micro_fade", m["direction"])

        for k, (w, l) in tally.items():
            if w + l >= 100:
                rates.setdefault(k, []).append(w / (w + l))
    return {k: statistics.mean(v) for k, v in rates.items()}, \
           {k: len(v) for k, v in rates.items()}


if __name__ == "__main__":
    for gen in ("mean_revert", "mixed", "random_walk", "trending"):
        means, counts = measure(gen)
        print(f"\n== {gen} ==")
        for k, v in sorted(means.items(), key=lambda x: -x[1]):
            print(f"  {k:<14}{v:.4f}  (seeds with n>=100: {counts[k]})")
