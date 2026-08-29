"""Walk-forward backtest of the FULL signal pipeline — the same
decision.evaluate() the live engine runs — over generated or stored
candles.

Why this exists: the per-source matrix in app/backtest.py measures each
source in isolation. It cannot tell you whether the ENGINE as a whole
picks the right direction, because weighting, regime scaling, and
vetoes only exist in the combination. This harness replays candle by
candle, grades every fired (confirmed) signal against the next candle's
close (the real binary-option outcome), and reports the overall win
rate plus how often the gates let a candle trade at all.

Data generators (--gen), all deterministic per seed:

  random_walk   driftless white-noise returns. An honest pipeline must
                score ~50% here — anything far off is a measurement bug.
  mean_revert   AR(1) with negative feedback to a slowly drifting anchor.
                The classic Quotex-OTC-style feed: ups follow downs.
  trending      persistent moves (positive AR + switching drift signs).
  mixed         alternating range/trend segments — the regime layer's
                home ground: its read has to flip with the segments.

Engine A/B: pass --repo-root pointing at a checkout of the OLD code
(e.g. a git worktree of the pre-fix commit) to run the identical data
through the identical harness on the old engine:

    python tools/backtest_engine.py --engine new --gen mean_revert
    git worktree add /tmp/mp-old HEAD   # the pre-fix commit
    python tools/backtest_engine.py --engine old \
        --repo-root /tmp/mp-old --gen mean_revert

--learn turns the per-pair weight learning loop on during the replay
(pattern_stats bumps + periodic weight-cache refresh), which is how the
live app runs; the default keeps every source unmeasured so the A/B
isolates raw analysis quality from the learning loop.
"""
import argparse
import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Synthetic candle generators — deterministic per (gen, seed)
# ---------------------------------------------------------------------------

def _finalize(prev_close: float, close: float, vol: float, rng: random.Random) -> dict:
    hi = max(prev_close, close) + abs(rng.gauss(0, 1)) * vol * 0.45
    lo = min(prev_close, close) - abs(rng.gauss(0, 1)) * vol * 0.45
    return {"open": prev_close, "high": hi, "low": lo, "close": close}


def gen_random_walk(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    price, vol = 1.10000, 0.00045
    out = []
    for _ in range(n):
        step = rng.gauss(0, vol)
        prev = price
        price += step
        out.append(_finalize(prev, price, vol, rng))
    return out


def gen_mean_revert(n: int, seed: int) -> list[dict]:
    """AR(1)-style pull toward a slowly wandering anchor: the next return
    tends to undo part of the last move — the OTC-style oscillation."""
    rng = random.Random(seed)
    price, vol = 1.10000, 0.00045
    anchor = 1.10000
    out = []
    for i in range(n):
        if i % 90 == 0:  # anchor wanders slowly, like session levels do
            anchor += rng.gauss(0, vol * 3)
        pull = -0.35 * (price - anchor)
        prev = price
        price += pull + rng.gauss(0, vol)
        out.append(_finalize(prev, price, vol, rng))
    return out


def gen_trending(n: int, seed: int) -> list[dict]:
    """Positively autocorrelated returns with drift segments that flip
    sign every couple of hours."""
    rng = random.Random(seed)
    price, vol = 1.10000, 0.00045
    drift = vol * 0.55
    out = []
    for i in range(n):
        if i % 110 == 0:
            drift = rng.choice([-1, 1]) * vol * rng.uniform(0.35, 0.8)
        prev = price
        price += drift + rng.gauss(0, vol)
        out.append(_finalize(prev, price, vol, rng))
    return out


def gen_mixed(n: int, seed: int) -> list[dict]:
    """Alternating range and trend segments of ~100 bars — the regime
    detector has to notice the switches for the family weights to pay."""
    rng = random.Random(seed)
    price, vol = 1.10000, 0.00045
    anchor = 1.10000
    drift = vol * 0.6
    out = []
    in_trend = False
    for i in range(n):
        if i % 100 == 0:
            in_trend = not in_trend
            drift = rng.choice([-1, 1]) * vol * rng.uniform(0.35, 0.8)
        prev = price
        if in_trend:
            price += drift + rng.gauss(0, vol)
        else:
            if i % 90 == 0:
                anchor = price + rng.gauss(0, vol * 2)
            price += -0.4 * (price - anchor) + rng.gauss(0, vol)
        out.append(_finalize(prev, price, vol, rng))
    return out


GENERATORS = {
    "random_walk": gen_random_walk,
    "mean_revert": gen_mean_revert,
    "trending": gen_trending,
    "mixed": gen_mixed,
}


# ---------------------------------------------------------------------------
# Walk-forward replay of the real pipeline
# ---------------------------------------------------------------------------

WARMUP = 130          # candles before the first decision (EMA50 needs 50+)
IN_MEMORY = 300       # mirror feed.py's in-memory window


async def run(engine: str, candles: list[dict], learn: bool) -> dict:
    from app import decision, db, indicators

    # Isolated throwaway database so the learning loop has somewhere to
    # write and no real deployment data is touched.
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "bt.db"
    await db.init_db()

    pair = "TEST"
    clean = [dict(c) for c in candles]

    # There is no more noise-tier fallback: evaluate() returns None on any
    # gate failure instead of a filler Decision, so every fired signal is
    # tier=="confirmed" and `skipped` is now the count of candles that
    # didn't earn a signal at all — the trade-frequency cost of dropping
    # the old ALWAYS_SIGNAL filler.
    wins = losses = 0
    draws = skipped = 0
    direction_counts = {"CALL": 0, "PUT": 0}

    for i in range(WARMUP, len(clean) - 1):
        history = clean[max(0, i - IN_MEMORY) : i + 1]
        current, following = clean[i], clean[i + 1]

        if following["close"] == current["close"]:
            draws += 1
            continue

        ind = indicators.compute(history)
        dec = await decision.evaluate(pair, history, ind)
        if dec is None:
            skipped += 1
            continue

        outcome_call = following["close"] > current["close"]
        won = (dec.direction == "CALL") == outcome_call
        wins += won
        losses += not won
        direction_counts[dec.direction] += 1

        if learn:
            # Mirror evaluator.py's grading. The NEW engine persists every
            # vote (both sides) and grades each on its own direction; the
            # OLD engine only ever graded the sources that made it onto
            # the fired signal (majority side) — the selection bias this
            # A/B is meant to expose.
            outcome_dir = "CALL" if outcome_call else "PUT"
            votes = getattr(dec, "all_votes", None)
            if votes is None:
                votes = [(s, dec.direction) for s in dec.sources]
            for source, direction in votes:
                await db.bump_pattern_stat(source, pair, direction == outcome_dir)
            if (i - WARMUP) % 25 == 0:
                # The live cache refreshes on a 55s wall-clock TTL; a fast
                # replay has to force it so the weights actually learn.
                decision._pattern_perf_cache_ts = 0.0

    traded = wins + losses
    return {
        "engine": engine,
        "bars": len(clean),
        "traded": traded,
        "draws": draws,
        "skipped": skipped,
        "win_rate": round(wins / traded, 4) if traded else None,
        "calls": direction_counts["CALL"],
        "puts": direction_counts["PUT"],
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward backtest of the full signal engine")
    ap.add_argument("--engine", choices=["new", "old"], default="new")
    ap.add_argument("--repo-root", default=None, help="path to the repo whose app package should load")
    ap.add_argument("--gen", choices=list(GENERATORS), default="random_walk")
    ap.add_argument("--bars", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--learn", action="store_true", help="run with the weight-learning loop active")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    # Make sure the intended app package is the one that loads.
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]

    candles = GENERATORS[args.gen](args.bars, args.seed)
    result = await run(args.engine, candles, args.learn)
    result["gen"] = args.gen
    result["seed"] = args.seed
    result["learn"] = args.learn

    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"[{args.engine}] gen={args.gen} seed={args.seed} learn={args.learn}")
    print(f"  bars={result['bars']} traded={result['traded']} skipped={result['skipped']} draws={result['draws']}")
    print(f"  win_rate = {result['win_rate']} (n={result['traded']})")
    print(f"  calls={result['calls']} puts={result['puts']}")


if __name__ == "__main__":
    asyncio.run(main())
