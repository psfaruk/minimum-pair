"""Measures every signal source on its own, against stored candles.

The live per-source table (`/api/patterns`) only sees sources that
actually made it onto a fired signal, in whatever direction the combined
vote settled on. That answers "how did the engine do", not "does this
strategy work" — a source can be right most of the time and never appear,
because something heavier outvoted it.

This replays the stored candles and asks each source the only question
that matters: when *you alone* said CALL, did the next candle close
higher? Every source is scored independently on identical data, so the
results are comparable, and the weight each has earned falls out of it.

Read-only: it never writes a signal, never touches pattern_stats, and
trains the miner into a local library so a run can't disturb the live
one.

CLI:  python -m app.backtest --pair "EUR/USD" --limit 5000
API:  GET /api/backtest?pair=EUR/USD&limit=5000
"""
import argparse
import asyncio
import json
from typing import Any

from app import candle_reaction, config, db, decision, indicators, microstructure, pattern_miner, weights
from app.stats import block_signflip_p_value, fdr_pass_mask, wilson_interval

Candle = dict[str, Any]

# Candles needed before the indicators mean anything (EMA_PERIOD is the
# longest window in indicators.compute).
WARMUP = 60

# The miner is retrained every this many candles on the preceding window,
# then only ever applied to candles it has not seen — otherwise it would
# be scored on the data it learned from, which flatters it enormously.
MINER_RETRAIN_EVERY = 30
MINER_TRAIN_WINDOW = 300

# False-discovery rate for the whole batch of sources tested in one run.
FDR_ALPHA = 0.10

# A real edge shows up across the whole period, not in one lucky stretch.
# Measured on driftless random walks, a single source's hit rate varies
# across runs about 1.4x more than the binomial formula predicts, so no
# single-sample test is enough on its own: the run is also cut into
# sequential folds and an edge has to hold in most of them.
FOLDS = 5
MIN_FOLD_SAMPLES = 20


def _outcome(current: Candle, following: Candle) -> str | None:
    """What a one-candle binary trade entered at `current`'s close would
    have paid. None means a draw — the stake comes back, so it is not
    evidence either way."""
    if following["close"] > current["close"]:
        return "CALL"
    if following["close"] < current["close"]:
        return "PUT"
    return None


def _sources_firing(pair: str, history: list[Candle], miner_library: dict) -> list[tuple[str, str]]:
    """Every (source, direction) that fires on this candle, before any
    weighting or vote aggregation."""
    firing: list[tuple[str, str]] = []

    ind = indicators.compute(history)
    for vote in decision._indicator_votes(ind):
        firing.append((vote.source, vote.direction))

    for name, direction in decision.patterns.detect(history):
        firing.append((name, direction))

    cr = candle_reaction.detect(history)
    if cr is not None:
        firing.append((cr[0], cr[1]))

    micro = microstructure.score(history)
    if micro["direction"] is not None:
        firing.append((microstructure.PATTERN_NAME, micro["direction"]))

    # The miner, applied out-of-sample against the local library.
    if miner_library and len(history) >= pattern_miner.SEQUENCE_LENGTH:
        key = pattern_miner._sequence_key(history[-pattern_miner.SEQUENCE_LENGTH :])
        entry = miner_library.get(key)
        if entry is not None:
            firing.append((f"{pattern_miner.PATTERN_PREFIX}_{key}", entry["direction"]))

    # The always-on filler, measured like everything else so its true
    # value (none) is visible rather than assumed.
    firing.append(("fallback_color", decision._fallback_vote(history).direction))

    return firing


async def backtest_pair(pair: str, limit: int = 5000) -> dict[str, Any]:
    """Scores every source independently over this pair's stored candles."""
    candles = await db.recent_candles(pair, limit)

    # Ordered hit/miss per source, not just counts: the significance test
    # needs the sequence to see how much the results cluster.
    tally: dict[str, list[bool]] = {}
    miner_library: dict = {}
    tested = skipped_synthetic = draws = 0

    for i in range(WARMUP, len(candles) - 1):
        history = candles[: i + 1]
        current, following = candles[i], candles[i + 1]

        # A fabricated candle is the last price repeated; neither a
        # signal from it nor an outcome read off it means anything.
        if current.get("synthetic") or following.get("synthetic"):
            skipped_synthetic += 1
            continue

        if (i - WARMUP) % MINER_RETRAIN_EVERY == 0:
            # Trained strictly on candles before this one.
            miner_library = pattern_miner._mine(history[-MINER_TRAIN_WINDOW:-1])

        outcome = _outcome(current, following)
        if outcome is None:
            draws += 1
            continue

        tested += 1
        for source, direction in _sources_firing(pair, history, miner_library):
            tally.setdefault(source, []).append(direction == outcome)

    # Two corrections, both necessary before calling anything an edge.
    # The p-value accounts for results arriving in clusters (see
    # block_signflip_p_value); FDR accounts for ~28 sources being screened
    # at once. Skip either one and this tool reports edges on data that
    # provably has none, and someone trades them.
    sources = list(tally)
    p_values = [block_signflip_p_value(tally[s]) for s in sources]
    survives_fdr = dict(zip(sources, fdr_pass_mask(p_values, FDR_ALPHA)))

    results = []
    for index, source in enumerate(sources):
        hits = tally[source]
        wins = sum(hits)
        losses = len(hits) - wins
        n = len(hits)
        lower, upper = wilson_interval(wins, n)
        fold_rates = _fold_rates(hits)
        results.append(
            {
                "source": source,
                "n": n,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / n if n else None,
                "wilson_lower": round(lower, 4),
                "wilson_upper": round(upper, 4),
                "p_value": round(p_values[index], 5),
                "survives_fdr": survives_fdr[source],
                "earned_weight": round(weights.weight_for(wins, losses), 3),
                "fold_rates": [round(r, 3) for r in fold_rates],
                "verdict": _verdict(n, lower, upper, survives_fdr[source], fold_rates),
            }
        )
    results.sort(key=lambda r: -(r["win_rate"] or 0))

    return {
        "pair": pair,
        "candles": len(candles),
        "candles_tested": tested,
        "skipped_synthetic": skipped_synthetic,
        "draws": draws,
        "sources": results,
    }


def _fold_rates(hits: list[bool]) -> list[float]:
    """Hit rate in each sequential slice of the run. An edge that only
    exists in one slice is a lucky stretch, not a strategy."""
    size = len(hits) // FOLDS
    if size < MIN_FOLD_SAMPLES:
        return []
    return [
        sum(hits[i * size : (i + 1) * size]) / size
        for i in range(FOLDS)
    ]


def _verdict(n: int, lower: float, upper: float, survives_fdr: bool, fold_rates: list[float]) -> str:
    """What the evidence actually supports — not the point estimate,
    which at these sample sizes is mostly noise; not an uncorrected
    interval, which finds edges in a batch of pure coin flips; and not a
    single-sample test, which a lucky stretch can carry on its own."""
    if n < 30:
        return "too few samples"
    consistent = sum(1 for r in fold_rates if r > 0.5) > len(fold_rates) / 2 if fold_rates else False
    if lower > 0.5 and survives_fdr and consistent:
        return "edge (better than chance)"
    if lower > 0.5 and survives_fdr:
        return "good overall, but not consistent across the period"
    if lower > 0.5:
        return "looks good, fails multiple-testing correction"
    if upper < 0.5:
        return "reliably worse than chance"
    return "no edge distinguishable from chance"


async def backtest_all(limit: int = 5000) -> dict[str, Any]:
    """Every pair, plus the only cross-check that really holds up.

    Within one pair, everything is measured on a single price path, and a
    path can simply happen to favour a strategy — on driftless random
    walks one run in six produced a source that looked like a genuine
    edge across every fold. Separate pairs are separate paths, so a
    source that shows an edge on several of them is evidence; a source
    that shines on exactly one is the same lucky path in a nicer suit.
    """
    reports = [await backtest_pair(pair, limit) for pair in config.ALL_PAIRS]

    across: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report["sources"]:
            entry = across.setdefault(
                row["source"], {"source": row["source"], "pairs_tested": 0, "pairs_with_edge": 0, "wins": 0, "n": 0}
            )
            if row["n"] >= 30:
                entry["pairs_tested"] += 1
            if row["verdict"].startswith("edge"):
                entry["pairs_with_edge"] += 1
            entry["wins"] += row["wins"]
            entry["n"] += row["n"]

    summary = [
        {
            **entry,
            "pooled_win_rate": round(entry["wins"] / entry["n"], 4) if entry["n"] else None,
            "earned_weight": round(weights.weight_for(entry["wins"], entry["n"] - entry["wins"]), 3),
        }
        for entry in across.values()
    ]
    summary.sort(key=lambda r: (-r["pairs_with_edge"], -(r["pooled_win_rate"] or 0)))
    return {"pairs": reports, "across_pairs": summary}


def _print_report(report: dict[str, Any]) -> None:
    print(f"\n{'=' * 78}")
    print(f"{report['pair']}  —  {report['candles_tested']} candles tested "
          f"({report['skipped_synthetic']} synthetic skipped, {report['draws']} draws)")
    print("=" * 78)
    if not report["sources"]:
        print("  no stored candles for this pair yet")
        return
    print(f"  {'source':<26}{'n':>6}{'win':>8}{'95% CI':>16}{'weight':>9}  verdict")
    for r in report["sources"]:
        ci = f"{r['wilson_lower']:.2f}-{r['wilson_upper']:.2f}"
        print(f"  {r['source']:<26}{r['n']:>6}{r['win_rate']:>7.1%}{ci:>16}"
              f"{r['earned_weight']:>9.2f}  {r['verdict']}")


def _print_summary(summary: list[dict[str, Any]]) -> None:
    if not summary:
        return
    print(f"\n{'=' * 78}")
    print("ACROSS PAIRS — a source is only credible if it works on more than one path")
    print("=" * 78)
    print(f"  {'source':<26}{'pairs':>7}{'w/ edge':>9}{'pooled n':>10}{'pooled':>9}{'weight':>9}")
    for r in summary:
        if not r["n"]:
            continue
        rate = f"{r['pooled_win_rate']:.1%}" if r["pooled_win_rate"] is not None else "-"
        print(f"  {r['source']:<26}{r['pairs_tested']:>7}{r['pairs_with_edge']:>9}"
              f"{r['n']:>10}{rate:>9}{r['earned_weight']:>9.2f}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Score every signal source against stored candles")
    parser.add_argument("--pair", help="one pair; omit to test every pair")
    parser.add_argument("--limit", type=int, default=5000, help="candles per pair (default 5000)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    await db.init_db()
    if args.pair:
        result = {"pairs": [await backtest_pair(args.pair, args.limit)], "across_pairs": []}
    else:
        result = await backtest_all(args.limit)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    for report in result["pairs"]:
        _print_report(report)
    _print_summary(result["across_pairs"])


if __name__ == "__main__":
    asyncio.run(_main())
