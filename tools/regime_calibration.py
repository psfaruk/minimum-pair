"""Empirical calibration check for regime.detect thresholds."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import regime
from tools.backtest_engine import GENERATORS

from collections import Counter

for gen in GENERATORS:
    counts = Counter()
    scores = []
    for seed in range(40):
        candles = GENERATORS[gen](400, seed)
        r = regime.detect(candles[-120:])
        counts[r["regime"]] += 1
        scores.append(r["score"])
    avg = sum(scores) / len(scores)
    print(f"{gen:12s} avg_score={avg:+.3f}  {dict(counts)}")
