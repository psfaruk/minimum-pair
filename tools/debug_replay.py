"""Quick regime-distribution + fallback-attribution debug on the
mean_revert generator — tells us whether the regime layer actually
reads "range" there and which fallback priority carries the direction."""
import asyncio
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest_engine import GENERATORS  # noqa: E402
from app import decision, db, indicators, regime  # noqa: E402
import tempfile  # noqa: E402


def anchor_disp(candles, lookback=60):
    closes = [c["close"] for c in candles[-lookback:]]
    mean = sum(closes) / len(closes)
    atr = sum(max(c["high"] - c["low"], 1e-12) for c in candles[-14:]) / 14
    return (candles[-1]["close"] - mean) / atr if atr else 0.0


async def main():
    gen = sys.argv[1] if len(sys.argv) > 1 else "mean_revert"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    db.DB_PATH = Path(tempfile.mkdtemp()) / "dbg.db"
    await db.init_db()

    candles = GENERATORS[gen](2000, seed)
    clean = [dict(c) for c in candles]
    regimes = Counter()
    fb_sources = Counter()
    fb_acc = Counter()
    pool_acc = Counter()
    WARMUP = 130

    for i in range(WARMUP, len(clean) - 1):
        hist = clean[max(0, i - 300) : i + 1]
        cur, nxt = clean[i], clean[i + 1]
        if nxt["close"] == cur["close"]:
            continue
        outcome = "CALL" if nxt["close"] > cur["close"] else "PUT"
        r = regime.detect(hist)
        regimes[r["regime"]] += 1
        ind = indicators.compute(hist)
        dec = await decision.evaluate("T", hist, ind)
        if dec is None:
            continue
        hit = dec.direction == outcome
        if dec.tier == "fallback":
            src = dec.sources[0] if dec.sources else "?"
            # attribute by priority: rebuild which branch fired
            fb_sources[src] += 1
            fb_acc["win" if hit else "loss"] += 1
            fb_acc[f"src:{src}"] += 1 if hit else 0
            fb_acc[f"srcn:{src}"] += 1
        else:
            pool_acc["win" if hit else "loss"] += 1

    print(f"gen={gen} seed={seed}")
    print("regimes:", dict(regimes))
    print("fallback wins/losses:", fb_acc.get("win", 0), "/", fb_acc.get("loss", 0))
    print("confirmed wins/losses:", pool_acc.get("win", 0), "/", pool_acc.get("loss", 0))
    print("fallback by lead source:")
    for src, n in fb_sources.most_common():
        w = fb_acc.get(f"src:{src}", 0)
        print(f"  {src:<30}{n:>5}  winrate={w / n:.3f}")


asyncio.run(main())
