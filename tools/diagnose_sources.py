"""Diagnose per-source win rates on generated random-walk data — every
source must sit at ~50% on driftless noise; anything consistently below
reveals an anti-edge in the detector itself."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest, db
from tools.backtest_engine import GENERATORS


async def main():
    gen = sys.argv[1] if len(sys.argv) > 1 else "random_walk"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "diag.db"
    await db.init_db()

    candles = GENERATORS[gen](1500, seed)
    for i, c in enumerate(candles):
        await db.save_candle("TEST", i * 60, c["open"], c["high"], c["low"], c["close"])

    report = await backtest.backtest_pair("TEST", limit=1500)
    print(f"gen={gen} seed={seed} candles={report['candles']} tested={report['candles_tested']}")
    print(f"  breakeven={report['breakeven_win_rate']}")
    print(f"  {'source':<28}{'n':>6}{'win':>8}{'CI-lo':>8}{'CI-hi':>8}")
    for r in report["sources"]:
        if r["n"] < 30:
            continue
        print(f"  {r['source']:<28}{r['n']:>6}{r['win_rate']:>8.1%}{r['wilson_lower']:>8.3f}{r['wilson_upper']:>8.3f}")


asyncio.run(main())
