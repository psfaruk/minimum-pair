# Prediction-Engine Audit — every problem found and how it was fixed

Audit scope: **every line** of the signal pipeline (decision, feed,
evaluator, db, weights, regime, htf, microstructure, candle_reaction,
patterns, pattern_miner, indicators, stats, backtest, server) plus the
API surface and the UI. The user's complaint: "প্রেডিকশন signals ভুল
হয়" — predictions are wrong; history and win-rate must be exactly
right; no fallback signals; no overrides; several high-confidence
strategies must agree; the engine must understand the market's
position.

Each finding below is tagged:

- [SIGNAL]  — directly produced wrong/low-quality signals
- [LEARN]   — corrupted what the engine learned (weights / confidence)
- [RATE]    — made the history / win-rate numbers wrong
- [API/UI]  — misled the consumer about what a signal means

---

## A. Why predictions were wrong — root causes

| # | Problem | Class | Fix |
|---|---------|-------|-----|
| A1 | **Per-candle guarantee flooded the app with signals** — `_fallback_decision()` guaranteed a signal on every candle of every pair (~23,000 rows/day at 16 pairs). Almost all were `fallback`-tier best guesses with no quality gate. The user's feed was dominated by noise, so "signals are wrong" was mostly "signals are filler". | SIGNAL | **Fallback tier removed entirely.** `evaluate()` now returns `None` unless every gate passes. No filler, ever. |
| A2 | **`random.choice` inside the live signal path** — the last-resort fallback vote was a literal coin flip recorded into history and graded. | SIGNAL / RATE | Coin-flip path removed from the live engine (`_fallback_vote` kept only as a backtest *baseline* so its true (lack of) value stays measurable). |
| A3 | **`MIN_CONFIRMATIONS = 1`** — a single idea cluster could stamp a signal "confirmed". Five correlated detectors on one candle (RSI oversold + BB bounce + support + hammer + rejection wick) are ONE idea, and that one idea was enough. | SIGNAL | New `MIN_CONFLUENCE_STRATEGIES` (default **2**, env-tunable): at least two independent strategy *families* must agree on the direction. |
| A4 | **Unmeasured sources could confirm** — when no graded history existed the confidence gate was skipped entirely ("an unmeasured signal is still a confirmed one"): brand-new pairs fired confident-looking signals on zero evidence. | SIGNAL | Two-tier gate: measured confluences must clear `MIN_CONFIDENCE` (0.65 shrunk); unmeasured ones must clear a much higher *structural* bar (`BOOTSTRAP_AGREEMENT` = 0.75 agreement dominance) — and once measured, a confluence below the bar goes **silent**. |
| A5 | **Confidence measured the wrong thing** — confidence was an average of per-source records. The engine never measured "how often does THIS KIND of confluence win on THIS pair", so it could not prefer its own best strategies. | LEARN / SIGNAL | **Signature learning (the core upgrade):** every signal stores its `signature` = `direction|regime|agreeing-families` — the stable name of a strategy the app composed itself. `signal_stats` learns that signature's own record per pair, and the engine gates future firings on it. The app literally keeps its winning strategies and silences its losing ones. |
| A6 | **Three post-hoc overrides contradicted the vote pool** — (1) a confident microstructure read vetoed the pool; (2) a wick-rejection (`candle_reaction`) vetoed the pool; (3) the "theory-opposed" check silently replaced the pool's direction in the fallback path. The user requirement is *no overrides*. | SIGNAL | All vetoes removed. Microstructure is now an ordinary **vote** (family `microstructure`, strength-scaled, learned per pair). Regime opposition became a pure **gate** (silence, never a flip). The pool's weighted answer is the only answer. |
| A7 | **No minimum-history gate** — signals fired with as few as ~2 candles of context, when every indicator/pattern/regime read is degraded. | SIGNAL | New gate: `MIN_HISTORY_CANDLES` (default 80) clean closed candles required before the engine may speak. |
| A8 | **Signals fired up to a full minute late** — a candle finalized late still fired a signal whose entry minute had already started; the recorded entry price no longer matched anything a follower could enter at, so the graded result described a trade nobody made. | RATE | `SIGNAL_MAX_LATE_SECONDS` (default 5s): later than that → no signal. History now only contains tradable calls. |
| A9 | **`_theory_opposed()` fall-through** — in a neutral regime the check silently fell through to the HTF comparison, a second, different rule with different thresholds, making the veto behaviour hard to reason about and impossible to unit-test cleanly. | SIGNAL | Replaced by the single explicit regime-alignment gate (trend regime ⇒ trend family must be on the winning side; range regime ⇒ reversion family). |
| A10 | **Microstructure's information never reached the pool** — it could only veto or serve as the fallback filler, so the aggregation never weighed its (often correct) read. | SIGNAL | It now votes with a strength-scaled weight and its per-pair record is learned like every other source. |
| A11 | **HTF-opposition only ran on the fallback path** — the confirmed path could fire a reversion-only direction straight into a strong 5-minute opposing leg. | SIGNAL | The regime-alignment gate applies to every confirmed signal; the HTF trend vote is additionally in the pool itself. |

## B. Why history / win-rate could be wrong

| # | Problem | Class | Fix |
|---|---------|-------|-----|
| B1 | **Double-grading race** — `grade_signal` had no `result='PENDING'` guard, so any second grading pass would re-grade the row and bump `pattern_stats` **twice** for one trade, silently corrupting every win rate downstream. | RATE | `UPDATE ... WHERE id=? AND result='PENDING'` returns whether the transition actually happened; stats are bumped **only** on a true transition (tested). |
| B2 | **No DB-level uniqueness on `(pair, entry_ts)`** — the application-level `signal_exists()` check raced finalize/restart replays; the historical feed race produced 2–4 signals for the same entry minute, each grading the same trade again. | RATE | One-time dedupe (keeps the first row — the one fired at the boundary) + **UNIQUE index** on `(pair, entry_ts)`. Duplicates are now impossible at the database layer (tested). |
| B3 | **Fallback signals polluted the learning loop** — fallback-tier decisions were graded like real signals, moving source weights and (pre-fix) win rates with no-gate noise. | LEARN / RATE | No fallback signals exist any more; only gate-passing signals are recorded, graded, and learned from. |
| B4 | **Stale signals looked live** — `/api/live` and `/api/signals` returned a pair's last signal (possibly hours old, already graded) with nothing distinguishing it from a fresh call; bots could trade an expired signal. | API/UI | Every row now carries `actionable` (entry minute still open AND pending — the only field a bot should trade on) and `expired`. The UI dims expired/graded cards and labels them "মেয়াদ শেষ". |
| B5 | **Draw semantics** — ties counted as losses once upon a time; already fixed earlier, verified intact: ties are DRAW (stake refunded), excluded from win-rate denominators, excluded from learning. | RATE | Verified + regression tests kept. |
| B6 | **Backtest warm-up too short** — `WARMUP = 60` while the regime read, BB-width percentile and fractal windows all need ~120 candles: the first ~60 scored candles used degraded indicator values, skewing every measured edge. | RATE | `WARMUP = 130`. |
| B7 | **Signature stats had no rebuild path** — introducing a new learning table on existing deployments requires a one-time recount consistent with the repaired grades. | RATE | `signal_stats` rebuilt once (after the tie/synthetic repairs) under an `app_state` flag, then maintained incrementally. |
| B8 | **`pending_signals_due` + grading loop could double-broadcast** — the graded event is now emitted only on a true transition. | API | Fixed with B1. |

## C. Engine-consistency and API fixes

| # | Problem | Fix |
|---|---------|-----|
| C1 | `backtest._sources_firing()` measured microstructure without the regime-fade flag the live engine uses — backtest and live disagreed on identical data. | Microstructure is now a normal vote in both (fade-aware in both). |
| C2 | `/api/strategies` advertised `fallback_color` as a live source and did not describe the engine's firing rules. | Registry updated + `engine` block describing confluence-v3 gates. |
| C3 | `state.last_regime` only updated when a signal fired — with signals now rare, `/api/status` regimes went stale. | Regime read refreshed on every finalized candle via `decision.last_regime_by_pair`. |
| C4 | UI copy promised "প্রতিটি ক্যান্ডেলে সিগন্যাল আসবে" — false under confluence-only firing and alarming to the user. | Live board shows a standby state ("অপেক্ষা…"), the empty-state explains silence-is-normal, and each card shows how many strategies agreed ("Nটি স্ট্র্যাটেজি একমত"). |
| C5 | `CODE_VERSION` stale. | Bumped to `2026.09.02-confluence-v3` so a deploy can be verified from `/api/status`. |
| C6 | Legacy `fallback`/`noise` tier rows remain in old databases. | All endpoints/UI still render them (labelled legacy); new rows are always `confirmed`. |
| C7 | The settings tab did not expose the new tunables. | Settings note lists `MIN_CONFLUENCE_STRATEGIES`, `MIN_CONFIDENCE`, `QUALITY_FLOOR` (+ `BOOTSTRAP_AGREEMENT`, `MIN_HISTORY_CANDLES`, `SIGNAL_MAX_LATE_SECONDS` remain env-tunable). |

## D. How the new engine decides

```
for each closed candle:
    gather votes from every source (indicators, 23 candlestick patterns,
        wick reactions, mined sequences, microstructure, streak fade,
        HTF alignment, anchor displacement)
    weight each vote by its own measured record on THIS pair
        (shrunk toward 50%, never 0, never hand-typed)
    scale by regime agreement (trend/range multiplier, bounded 0.4–1.6x)
    cap correlated pile-ups: one idea (family+direction), one vote

    winning side = larger total weight
    FIRE the signal only if ALL gates pass:
      0. >= 80 clean closed candles of history
      1. a net direction exists
      2. >= MIN_CONFLUENCE_STRATEGIES (2) distinct strategy families agree
      3. winning side carries >= QUALITY_FLOOR (0.65) of total weight
      4. regime alignment: the live regime's family is on the winning side
      5. confidence:
           - this exact signature measured >= 40 graded outcomes:
               its shrunk win rate must be >= MIN_CONFIDENCE (0.65)
           - else the source-mix measured (>= 40): same bar
           - else (unmeasured): agreement dominance >= 0.75 (bootstrap)
      otherwise: SILENCE — no fallback, no filler, no override.
```

Every fired signal is graded at the close of its entry minute against the
price a follower actually entered at (late-tick corrections re-price the
entry before grading). Draws (exact ties) are refunds: excluded from win
rates and from learning. One entry minute can only ever hold one signal
(unique index). One grade can only ever count once (transition guard).

## E. Verification

- `tests/test_engine_fixes.py` — 13 tests, incl. the four new gate tests,
  signature learning, double-grade guard, unique-index guard.
- `tests/test_feed_fixes.py` — feed-race scenarios incl. the duplicate
  rejection and the no-re-grade guard.
- `tests/test_quotex_client_fixes.py` — unchanged, passing.
- `tools/backtest_engine.py` — full-pipeline walk-forward replay on four
  synthetic market types (random walk / mean-reverting / trending /
  mixed) × multiple seeds; see the commit message for the result matrix.
