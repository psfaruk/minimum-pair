"""Multi-seed baseline A/B harness — runs the walk-forward engine backtest
across several seeds and reports per-generator mean win rates, so a change
can be judged on evidence rather than one lucky path.

Usage:
  python tools/ab_harness.py --engine new [--repo-root PATH] [--learn]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.backtest_engine import GENERATORS, run  # noqa: E402

SEEDS = [7, 11, 23, 42, 99]
GENS = ["mean_revert", "trending", "mixed", "random_walk"]
BARS = 2000


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["new", "old"], default="new")
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = []
    for gen in GENS:
        for seed in SEEDS:
            candles = GENERATORS[gen](BARS, seed)
            r = await run(args.engine, candles, args.learn)
            rows.append({"gen": gen, "seed": seed, **r})

    def agg(gen, key):
        vals = [r[key] for r in rows if r["gen"] == gen and r[key] is not None]
        return sum(vals) / len(vals) if vals else None

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    print(f"[{args.engine}] learn={args.learn} seeds={SEEDS} bars={BARS}")
    print(f"{'generator':<14}{'all':>8}{'conf':>8}{'n_conf':>9}{'fall':>8}{'n_fall':>9}")
    for gen in GENS:
        print(
            f"{gen:<14}"
            f"{agg(gen, 'win_rate_all'):>8.4f}"
            f"{agg(gen, 'win_rate_confirmed'):>8.4f}"
            f"{agg(gen, 'n_confirmed'):>9.0f}"
            f"{agg(gen, 'win_rate_fallback'):>8.4f}"
            f"{agg(gen, 'n_fallback'):>9.0f}"
        )
    overall_all = sum(r["win_rate_all"] for r in rows) / len(rows)
    print(f"\noverall mean win_rate_all = {overall_all:.4f}")
    # save detailed rows for comparison
    out = Path(f"/tmp/ab_{args.engine}_{'learn' if args.learn else 'raw'}.json")
    out.write_text(json.dumps(rows, indent=2))
    print(f"detail -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
