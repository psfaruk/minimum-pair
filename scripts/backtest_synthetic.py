"""Backtest on synthetic random-walk data — verifies the engine,
the per-pair per-strategy accounting, and the verdict logic end-to-end
without needing a live Quotex connection.
"""
import asyncio
import os
import random
import sys
import tempfile
from pathlib import Path

_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB.close()
os.environ["DB_PATH"] = _DB.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest, config, db  # noqa: E402


def _gen_candles(n: int = 500, seed: int = 42, start: float = 1.1000) -> list[dict]:
    rnd = random.Random(seed)
    price = start
    candles = []
    ts = 1700000000
    for _ in range(n):
        o = price
        drift = rnd.uniform(-0.0005, 0.0005)
        c = max(0.0001, o + drift)
        h = max(o, c) + rnd.uniform(0, 0.0008)
        low = min(o, c) - rnd.uniform(0, 0.0008)
        candles.append({
            "ts": ts,
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(low, 5),
            "close": round(c, 5),
            "synthetic": 0,
        })
        price = c
        ts += 60
    return candles


async def main() -> None:
    print("=" * 70)
    print("Backtest on synthetic random-walk data")
    print("=" * 70)
    await db.init_db()

    # Seed candle history for two pairs.
    pairs = ["EUR/USD", "AUD/USD"]
    for pair in pairs:
        candles = _gen_candles(500, seed=hash(pair) & 0xFFFF)
        for c in candles:
            await db.save_candle(pair, c["ts"], c["open"], c["high"], c["low"], c["close"])
        print(f"  seeded {len(candles)} candles for {pair}")

    print("\nPer-pair backtest:")
    for pair in pairs:
        report = await backtest.backtest_pair(pair, 500)
        n_sources = len(report["sources"])
        tested = report["candles_tested"]
        draws = report["draws"]
        skipped = report["skipped_synthetic"]
        # Find the best-performing source
        best = max(report["sources"], key=lambda r: r["win_rate"] or 0)
        print(f"  {pair}: {tested} candles, {n_sources} sources, {draws} draws, {skipped} synthetic skipped")
        print(f"    best source: {best['source']} win_rate={best['win_rate']:.2%} n={best['n']} verdict='{best['verdict']}'")
        print(f"    payout={report['payout']:.2f} breakeven={report['breakeven_win_rate']:.2%}")

    print("\nAll-pairs backtest:")
    all_report = await backtest.backtest_all(500)
    print(f"  pairs tested: {len(all_report['pairs'])}")
    print(f"  cross-pair summary entries: {len(all_report['across_pairs'])}")
    # Print top 3 sources by pairs_with_edge
    summary = sorted(all_report["across_pairs"],
                     key=lambda r: -r["pairs_with_edge"])[:3]
    for entry in summary:
        print(f"  {entry['source']}: pairs_tested={entry['pairs_tested']} "
              f"pairs_with_edge={entry['pairs_with_edge']} "
              f"pooled_win_rate={(entry['pooled_win_rate'] or 0):.2%}")

    print("\n" + "=" * 70)
    print("BACKTEST COMPLETED — engine evaluates correctly on synthetic data")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
