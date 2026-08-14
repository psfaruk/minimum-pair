"""Self-learned candle-sequence pattern miner.

Encodes each closed candle into a coarse shape symbol, groups the last
K-candle sequences seen historically, and checks whether the candle that
followed each sequence tended to close in a particular direction more
often than chance. A sequence is only promoted into the active library
if it passes two statistical bars:
  1. Benjamini-Hochberg FDR correction across every sequence tested this
     round (we're running many simultaneous hypothesis tests — mining
     hundreds of candidate sequences — so an uncorrected p-value would
     hand-pick lucky noise).
  2. A Wilson score lower-bound (95% CI) of at least 0.53 — the point
     estimate has to be good even in the pessimistic case, not just on
     average.

Retrains per pair every RETRAIN_EVERY newly closed candles.
"""
from typing import Any

from app.candle_shape import body_ratio, is_bull
from app.stats import binom_p_value_one_sided, fdr_pass_mask, wilson_lower_bound

Candle = dict[str, Any]

PATTERN_PREFIX = "mined"
# Sizing note: with a 300-candle in-memory window, the (alphabet_size ^
# SEQUENCE_LENGTH) space has to stay small enough that MIN_SAMPLES worth
# of repeats can plausibly occur. Wick shape is deliberately left out of
# the encoding (color+size only -> 4 symbols) since wick-based rejection
# is already covered with proper context by patterns.py/candle_reaction.py
# — folding it in here just fragments the sequence space until nothing
# ever reaches significance. 4^2=16 sequences over ~300 candles gives an
# expected ~19 samples per sequence, comfortably above MIN_SAMPLES.
SEQUENCE_LENGTH = 2
MIN_SAMPLES = 8
WILSON_THRESHOLD = 0.53
FDR_ALPHA = 0.10
RETRAIN_EVERY = 30

_libraries: dict[str, dict[str, dict[str, Any]]] = {}
_candles_since_retrain: dict[str, int] = {}


def _encode_candle(c: Candle) -> str:
    color = "U" if is_bull(c) else "D"
    size = "L" if body_ratio(c) >= 0.5 else "S"
    return f"{color}{size}"


def _sequence_key(candles: list[Candle]) -> str:
    return "-".join(_encode_candle(c) for c in candles)


def _mine(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    occurrences: dict[str, list[bool]] = {}  # key -> [True if the graded outcome was CALL]
    for i in range(SEQUENCE_LENGTH - 1, len(candles) - 1):
        window = candles[i - SEQUENCE_LENGTH + 1 : i + 1]
        # A fabricated candle carries no information — it's the last price
        # repeated because the minute had no ticks. Training on it teaches
        # the miner that quiet minutes are bearish (a flat candle encodes
        # as "down"), which is an artefact of the feed, not the market.
        if any(c.get("synthetic") for c in window) or candles[i + 1].get("synthetic"):
            continue
        key = _sequence_key(window)
        # Learn the exact event the evaluator scores: did the next candle
        # close above *this* candle's close. The old label was
        # is_bull(next) — close above its own open — which is a different
        # question whenever open[t+1] != close[t], and the two disagreed
        # 5.4% of the time on measured data.
        outcome_call = candles[i + 1]["close"] > candles[i]["close"]
        occurrences.setdefault(key, []).append(outcome_call)

    candidates = []
    for key, outcomes in occurrences.items():
        n = len(outcomes)
        if n < MIN_SAMPLES:
            continue
        ups = sum(1 for o in outcomes if o)
        downs = n - ups
        direction = "CALL" if ups >= downs else "PUT"
        wins = ups if direction == "CALL" else downs
        p_value = binom_p_value_one_sided(wins, n)
        candidates.append({"key": key, "direction": direction, "wins": wins, "n": n, "p_value": p_value})

    if not candidates:
        return {}

    passed_mask = fdr_pass_mask([c["p_value"] for c in candidates], FDR_ALPHA)

    library: dict[str, dict[str, Any]] = {}
    for candidate, passed in zip(candidates, passed_mask):
        if not passed:
            continue
        lower = wilson_lower_bound(candidate["wins"], candidate["n"])
        if lower < WILSON_THRESHOLD:
            continue
        library[candidate["key"]] = {
            "direction": candidate["direction"],
            "win_rate": candidate["wins"] / candidate["n"],
            "wilson_lower": lower,
            "n": candidate["n"],
        }
    return library


def maybe_retrain(pair: str, candles: list[Candle]) -> None:
    """Call once per newly closed candle for this pair; retrains the
    mined-pattern library every RETRAIN_EVERY candles.

    The first call for a pair trains immediately off whatever bootstrap
    history is already in memory (often 100-200 candles) instead of
    waiting RETRAIN_EVERY *new* closes to accumulate — otherwise the
    miner would sit idle for ~30 minutes after every server start even
    though it already has plenty of history to learn from.
    """
    if pair not in _candles_since_retrain and len(candles) >= SEQUENCE_LENGTH + MIN_SAMPLES:
        _libraries[pair] = _mine(candles)
        _candles_since_retrain[pair] = 0
        return

    count = _candles_since_retrain.get(pair, 0) + 1
    if count >= RETRAIN_EVERY and len(candles) >= SEQUENCE_LENGTH + MIN_SAMPLES:
        _libraries[pair] = _mine(candles)
        count = 0
    _candles_since_retrain[pair] = count


def predict(pair: str, candles: list[Candle]) -> tuple[str, str, float] | None:
    """Checks whether the most recent SEQUENCE_LENGTH candles match a
    promoted pattern for this pair. Returns (name, direction, win_rate)."""
    library = _libraries.get(pair)
    if not library or len(candles) < SEQUENCE_LENGTH:
        return None
    key = _sequence_key(candles[-SEQUENCE_LENGTH:])
    entry = library.get(key)
    if entry is None:
        return None
    return f"{PATTERN_PREFIX}_{key}", entry["direction"], entry["win_rate"]


def library_size(pair: str) -> int:
    return len(_libraries.get(pair, {}))
