"""Seeds a throwaway DB with realistic-looking demo data so the UI can
be screenshot-verified without a live Quotex connection. NEVER points at
a real DB_PATH — the path comes from argv."""
import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402

PAIRS = ["EUR/USD", "GBP/USD", "USD/BDT OTC", "USD/INR OTC", "USD/BRL OTC",
         "USD/PKR OTC", "NZD/USD OTC", "USD/ZAR OTC", "USD/JPY", "AUD/USD",
         "EUR/GBP", "USD/MXN OTC", "USD/IDR OTC", "USD/COP OTC", "USD/DZD OTC", "USD/PHP OTC"]
SOURCES = ["rsi_oversold", "bb_lower_bounce", "near_support", "streak_exhaustion",
           "anchor_fade", "ema_trend_up", "microstructure", "htf_trend", "hammer"]


async def main() -> None:
    db.DB_PATH = Path(sys.argv[1])
    await db.init_db()
    rng = random.Random(42)
    now = int(time.time())
    n = 0
    for pair in PAIRS:
        price = rng.uniform(0.6, 1.25)
        # 240 candles of 1m history
        for i in range(240, 0, -1):
            ts = now - i * 60
            o = price
            price += rng.gauss(0, 0.0006)
            c = price
            hi = max(o, c) + abs(rng.gauss(0, 0.0004))
            lo = min(o, c) - abs(rng.gauss(0, 0.0004))
            await db.save_candle(pair, ts, o, hi, lo, c)
            n += 1
        # 180 signals over the last 3 hours, ~55% win rate
        for i in range(180, 0, -1):
            created = now - i * 60
            direction = rng.choice(["CALL", "PUT"])
            tier = "confirmed" if rng.random() < 0.3 else "fallback"
            base = 0.58 if tier == "confirmed" else 0.52
            roll = rng.random()
            if roll < base:
                result = "WIN"
            elif roll < base + 0.04:
                result = "DRAW"
            else:
                result = "LOSS"
            if i <= 2:
                result = "PENDING"
            entry = created + 60
            await db.insert_signal(
                pair=pair,
                direction=direction,
                entry_ts=entry,
                target_close_ts=entry + 60,
                confidence=round(rng.uniform(0.66, 0.85), 3) if tier == "confirmed" else None,
                source=rng.choice(SOURCES),
                entry_price=round(price + rng.gauss(0, 0.0002), 5),
                tier=tier,
            )
            # grade it directly
            import sqlite3
            with db._connect() as conn:
                conn.execute(
                    "UPDATE signals SET result = ?, close_price = ? WHERE pair = ? AND entry_ts = ?",
                    (result, round(price + rng.gauss(0, 0.001), 5), pair, entry),
                )
            n += 1
    print(f"seeded {n} rows into {db.DB_PATH}")


asyncio.run(main())
