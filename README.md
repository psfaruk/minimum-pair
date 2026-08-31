# minimum-pair

## Engine v2: correlated votes, higher-timeframe context, measured fallback (2026-09-01)

Walk-forward A/B on 5 seeds × 4 synthetic generators (`tools/ab_harness.py`,
2000 bars each; `--learn` mirrors the live grading loop). Summary of the
multi-seed mean `win_rate_all`:

| generator | old engine (learn) | new engine (learn) | old engine (raw) | new engine (raw) |
|---|---|---|---|---|
| mean_revert (OTC-style) | 55.5% | **57.3%** | 50.2% | **52.1%** |
| trending | 67.6% | **68.0%** | 62.5% | **65.7%** |
| mixed | 58.6% | **59.4%** | 54.2% | **58.0%** |
| random_walk (honesty check) | 49.5% | 49.9% | 49.5% | 49.8% |
| **overall** | 57.8% | **58.6%** | 54.1% | **56.4%** |

The random-walk row is the honesty row: on driftless noise the engine
must stay at ~50%, and it does — every point of the gains above comes
from real structure, not from luck. Six root causes were found and
fixed this round:

1. **Correlated votes were counted as independent confirmations.** One
   physical event — price bouncing off the floor — fired `rsi_oversold`
   AND `bb_lower_bounce` AND `near_support` AND up to four rejection
   detectors AND a candlestick pattern, all on the same candle. Five
   "confirmations", one idea. In a downtrend those correlated reversion
   votes systematically outvoted the single honest trend vote and the
   signal was stamped `confirmed` on the wrong side. Votes are now
   grouped by (strategy family, direction): the first vote in a cluster
   carries its full earned weight, each additional vote in the same
   cluster decays (`cap/(cap+contributed)`), and `confirmations` counts
   distinct IDEAS, not detectors. A lone idea still needs to have
   measurably earned `LONE_VOTE_MIN_WEIGHT` to confirm.

2. **The fallback followed wrong-theory reads.** On the mean-reverting
   OTC-style feed the fallback direction was led ~20% of the time by
   `ema_trend_*` votes (a trend theory) that won only 32–43% of those
   trades. New rule — the theory-opposition veto: when a confirmed
   regime (strength ≥ 0.30) says the market is ranging but the winning
   side is supported ONLY by trend-family votes (or the mirror image),
   the pool's answer is skipped in the fallback path and demoted from
   the confirmed tier.

3. **A reversion fade fighting a live higher-timeframe leg.** RSI-extreme
   lone-vote fallbacks fired against a running 5-minute trend won ~30%
   of them (measured on trending + mixed feeds). New HTF-opposition
   check: outside a range regime, a direction supported only by
   reversion-family votes while the 5-minute leg clearly runs the other
   way (strength ≥ 0.40) is vetoed from confirming and skipped in the
   fallback chain.

4. **No higher-timeframe context at all.** New `app/htf.py` aggregates
   closed 1-minute candles into 5-minute buckets and reads position vs
   the HTF EMA, EMA slope, and net displacement — one honest
   `htf_trend` vote (trend family), and a better-than-colour fallback
   baseline when the 5-minute leg is clearly travelling.

5. **The two strongest simple edges on OTC-style feeds had no dedicated
   source.** Measured raw (`tools/measure_edges.py`): fading price
   stretched ≥ 1 ATR from its 60-candle mean won 65.2%; fading a 3+
   same-colour streak won 64.6% — while on trending feeds the SAME
   displacement read wins by FOLLOWING (68%). New regime-conditional
   sources: `anchor_fade` (range/neutral: fade displacement),
   `anchor_follow` (confirmed trend: follow it), and a strengthened
   `streak_exhaustion` fade. In the fallback chain, the last-resort
   colour read is now INVERTED when the regime says the market fades
   moves (colour-follow wins ~45% there — the fade wins ~55%).

6. **Confidence overstated small samples.** A 25-sample rate gated
   `confirmed`. Now `CONFIDENCE_MIN_SAMPLES = 40` and the reported
   confidence is the Beta posterior mean (`weights.shrunk_rate`), so a
   30-for-40 run reports ~58%, not 75%.

Also new: `/api/winrate` now returns per-pair CALL/PUT and
confirmed/fallback splits (plus a `days` window param),
`/api/winrate/summary` the global rollup, and `/api/history` gained
`direction`/`tier`/`result`/`offset` filters. The frontend was rebuilt
from scratch (2026-09 design system): a live signal board with
per-pair countdown cards and CALL/PUT hero bars, a stats view with the
per-pair × per-direction win-rate table, a filterable history, and a
cleaned-up chart — Bengali-first UI, `tools/seed_demo.py` seeds a demo
DB for offline UI verification.

## Per-candle guarantee, honestly tiered (2026-08-30)

Two requirements landed back to back, pulling in opposite directions:
"এত পরিমাণে প্রেডিকশন হয় যা অগ্রহণযোগ্য... শুধুমাত্র ট্রেডিং স্ট্রাটেজি
দিয়ে সিগন্যাল আসবে" (too much prediction volume, unacceptable — only
real trading-strategy signals should fire) briefly removed the always-on
filler entirely (`decision.evaluate()` returned `None` on any gate
failure). Right after, "প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে" (a
signal must come on every candle) asked for the guarantee back. Both are
satisfied together the same way the engine originally did it, just with
clearer terminology:

- Every real finalized candle still fires a signal — `decision.evaluate()`
  only returns `None` for the very first bootstrap call (zero candles).
  Every other path — insufficient confirmations, conflicting votes, an
  unproven lone vote, a microstructure/wick-rejection veto, structural
  weight below `QUALITY_FLOOR`, or measured confidence below
  `MIN_CONFIDENCE` — routes through `decision._fallback_decision()`
  instead, which always returns a `Decision`.
- Every signal carries a `tier`: `confirmed` passed every quality gate;
  `fallback` is the best-effort filler that carries no confidence and
  is no better than the engine's best guess. The fallback direction is
  the ensemble's own regime-weighted net read when one exists, falling
  through to microstructure, then a literal wick rejection, then plain
  last-candle colour only when nothing else fired at all — never a pure
  coin flip while there's a real read available. Poll
  `/api/signals?tier=confirmed` to see only the gated ones.
- (Pre-2026-08-29 rows, from a short-lived version of this filler, may
  carry the old tier name `noise` for the same fallback concept — same
  meaning, different label.)

## Why signals were wrong — and what changed (2026-08, engine rebuild)

A walk-forward A/B of the FULL pipeline (`tools/backtest_engine.py`,
identical data through the old and the new engine) measured the old
engine at **45.7%** on an OTC-style mean-reverting feed — below the
~54% breakeven, i.e. guaranteed loss — and the rebuilt engine at
**56.8%** on the same data, with the same honesty preserved on
driftless noise (~50%). Six root causes were found and fixed:

1. **No regime detection (wrong theory applied).** The vote pool mixed
   mean-reversion sources (RSI extremes, band bounces, wick
   rejections, S/R fades) with trend-following sources (EMA/SMA
   alignment, marubozu continuation, squeeze breakouts) and never asked
   which condition the pair was actually in. In a trend, reversal votes
   fire repeatedly against the move; in a range, trend votes enter
   after the move is exhausted. NEW `app/regime.py` reads the market
   first — Kaufman efficiency ratio + lag-1 autocorrelation of returns
   → `trend` / `range` / `neutral` with a strength — and every vote is
   weighed by how well its theory matches that condition (matching
   family up to 1.6x, opposing family down to 0.4x, never zero).

2. **The noise filler threw away the engine's own answer.** When the
   quality gates failed (most OTC candles), `_noise_decision()`
   discarded the computed net direction and answered with
   last-candle-colour following — worth exactly 50% and the single
   biggest visible source of wrong predictions, because
   `ALWAYS_SIGNAL` means most displayed signals ARE fillers. The filler
   now carries the ensemble's best direction (regime-weighted,
   measured-weighted), with regime-aware fallbacks behind it.

3. **`bb_squeeze` was always true.** The test compared the raw band
   width ratio (0.0002–0.003 on FX scales) against 0.5 — every candle
   "squeezed", so "squeeze breakout" really meant "any close outside
   the 2σ band". A squeeze now means the width sits in the tightest
   quartile of its own recent history.

4. **Fractal S/R was too dense.** Levels from the last 120 candles
   produced hundreds of swing points; some wick touched *some* level on
   nearly every candle, letting the rejection detector drive or veto
   the whole engine. Both the indicator S/R and the rejection detector
   now use only the most recent swings (`recent_fractal_levels`, 6 per
   side).

5. **Learning was selection-biased.** The evaluator graded only the
   sources that made it onto the fired signal (the majority side) — a
   source that kept voting the wrong direction but losing the weight
   contest never accumulated losses and was never demoted. Every signal
   now persists ALL votes in `signals.all_sources` (JSON) and every
   vote is graded on its OWN direction; `pattern_stats` was recounted
   once under the new rule (`pattern_stats_allvotes_v3`).

6. **A lone unmeasured vote could confirm.** `MIN_CONFIRMATIONS=1` plus
   a structural weight of 1.0 meant any single detector firing alone
   produced a "confirmed" signal. A lone vote now confirms only when
   its source has measured above `LONE_VOTE_MIN_WEIGHT` (0.80) on that
   pair; otherwise it still fires — as best-effort noise carrying the
   same direction.

Extras: the microstructure fallback now FADES the last move when the
regime reads a confident range (on mean-reverting OTC feeds,
colour-following is systematically wrong; the fade is the theory the
feed is exhibiting), `/api/status` exposes per-pair `regimes`, the WS
signal payload carries `regime`, and noise signals are best-effort but
still honestly tiered — poll `/api/signals?tier=confirmed` for only the
gated ones.

Verification tooling (committed): `tools/backtest_engine.py` (walk-
forward full-pipeline A/B over deterministic synthetic feeds:
random_walk / mean_revert / trending / mixed, `--learn` to run the
weight-learning loop), `tools/regime_calibration.py` (regime threshold
calibration against driftless noise), `tools/diagnose_sources.py`
(per-source win rates on generated data), and
`tests/test_engine_fixes.py` (unit coverage for all six fixes).

## Token-only auth surface (2026-08)

The app authenticates against Quotex **only** through a session token
(SSID) pasted into the Settings tab. There is no admin passcode, no
Railway API token, no other API key exposed in the frontend — the
entire auth story is "paste a token, the app goes live".

```
POST /api/session
  body: {"session_token": "<SSID>", "session_cookies": "<optional cookie header>"}
```

`session_cookies` is optional — most sessions work with just the
SSID. Set it when the broker requires `cf_clearance` or
`laravel_session` cookies alongside the SSID for the websocket
handshake to complete.

The pasted token is stored in the SQLite-backed `app_state` table
(`DB_PATH`), so it survives a container restart.

## No email/password login path

There is no email/password login at all — not as a fallback, not as
a one-shot bootstrap. The app never touches the Cloudflare-guarded
login page, which is also what used to get the shared egress IP
blocked when it was retried. A missing or dead token simply means the
app waits.

| State | Behaviour |
| --- | --- |
| Fresh boot, no token | `auth_mode` is `no_token`. The app waits, re-checking every ~20s, for one to be pasted. |
| Token pasted via `/api/session` | `auth_mode` flips to `session_token`, the feed connects immediately. |
| Mid-run token dies | The connection watchdog notices the silent feed and rebuilds the connection — reusing the same pasted token. |

`/api/status` exposes `auth_mode` so the Settings tab can show
exactly where the app is in this cycle.

## Per-pair, per-candle signal guarantee

Per the user requirement (*"প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ
সিগন্যাল আসতে হবে"*), every real finalized candle produces a
signal — either `confirmed` (passed the quality gates) or `fallback`
(the best-effort filler, see "Per-candle guarantee, honestly tiered"
at the top of this file for how the two coexist without the volume
complaint that first triggered removing this guarantee).

A signal is fired at the **0-second mark** when a new candle opens
(computed from the just-closed previous candle). The chart view
shows every signal as a coloured arrow on its candle (green up-arrow
for CALL, red down-arrow for PUT), so the per-candle promise is also
visible.

## Open public signal API

Anyone can fetch the current CALL/PUT signal for every pair, no auth,
no WebSocket — just an HTTP GET. Per the user requirement: open URLs,
no auth, suitable for external bots/scripts to consume directly.
CORS is open (`Access-Control-Allow-Origin: *`).

```
GET /api/signals                       # all pairs' latest signal
GET /api/signals?tier=confirmed        # only quality-gated signals
GET /api/signals/pair?pair=EUR/USD     # one pair's latest signal
GET /api/signals/history?pair=EUR/USD  # one pair's signal history
GET /api/strategies                    # registry of every strategy the engine knows
GET /api/backtest?pair=EUR/USD         # per-pair per-strategy backtest matrix
GET /api/candles?pair=EUR/USD          # raw candle history
GET /api/winrate                       # per-pair win/loss tally
GET /api/status                        # service status (auth, reconnects, feed health)
```

Each signal row includes:

```json
{
  "pair": "EUR/USD",
  "direction": "CALL",
  "confidence": 0.62,
  "tier": "confirmed",
  "result": "PENDING",
  "entry_ts": 1786821000,
  "target_close_ts": 1786821060,
  "entry_price": 1.1005,
  "close_price": null,
  "source": "rsi_oversold,hammer",
  "sources": ["rsi_oversold", "hammer"],
  "created_at": 1786820940,
  "age_seconds": 60,
  "stale": false,
  "expires_in_seconds": 0
}
```

The signal's lifetime is exactly one 1-minute candle: it fires at the
**0-second mark** when a new candle opens (computed from the
just-closed previous candle), and the `target_close_ts` is the close
of that running candle. `expires_in_seconds` tells a bot exactly how
long the entry is still valid for.

## Per-pair per-strategy backtest matrix

The user requirement: *"যত গুলো পেয়ার ও স্ট্রাটেজি ব্যবহার করা
হয়েছে, প্রত্যেকটি কে আলাদা আলাদা পেয়ার এর সাথে ব্যাক টেস্ট করতে
হবে।"* `/api/backtest` produces, for every (pair, source) cell:

- `n`, `wins`, `losses`, `win_rate`
- `wilson_lower`, `wilson_upper` — 95% Wilson score CI
- `p_value` — block sign-flip p-value (cluster-aware, not the naive binomial)
- `survives_fdr` — Benjamini-Hochberg correction across the whole batch
- `longest_losing_streak` — worst drawdown length
- `payout`, `breakeven_win_rate` — per-pair payout → breakeven rate
  (`p_be = 1 / (1 + payout)`, per binaryoptions.ae)
- `roi_per_trade` — `win_rate * payout - (1 - win_rate)`; positive = real edge
- `fold_rates` — sequential 5-fold consistency check
- `verdict` — one of:
  - `"edge (better than breakeven, consistent across folds, FDR-surviving)"`
  - `"above breakeven, FDR-surviving, but not consistent across folds"`
  - `"above breakeven (X%), fails multiple-testing correction"`
  - `"reliably below breakeven (X%)"`
  - `"reliably worse than chance (50%)"`
  - `"no edge distinguishable from chance"`
  - `"too few samples"`

The `across_pairs` summary tells you which strategies have an edge on
*more than one* pair (the only test that survives the lucky path
problem on synthetic data). Per-pair payouts are configurable in
`config.PAIR_PAYOUT`.

## Expanded candlestick pattern library

The original engine covered 7 patterns. The web-research audit
identified that:

1. **Conflated shapes produce wrong-direction calls.** Hammer vs
   Hanging Man are the same shape with opposite context — the old
   `_hammer` fired "CALL" whenever the shape appeared, even after an
   uptrend (where it's a Hanging Man → PUT). Same for Inverted Hammer
   vs Shooting Star, and for Dragonfly vs Gravestone Doji. Now they
   are separated and require the correct prior-trend context.
2. **Missing high-value patterns.** Investopedia ranks Engulfing +
   3-candle stars + Three Soldiers/Crows as the most reliable
   reversal signals. These are now implemented: Piercing Line, Dark
   Cloud Cover, Morning Star, Evening Star, Three White Soldiers,
   Three Black Crows, Three Inside Up/Down, Three Outside Up/Down,
   Harami Cross.
3. **Doji sub-types.** A doji is not just "indecision" — Dragonfly,
   Gravestone, and Long-Legged have meaningfully different reversal
   bias depending on prior trend.

The full registry (31 patterns) is exposed at `GET /api/strategies`.

## Expanded candle-reaction family

The original `candle_reaction` source detected wick rejection at
fractal swing S/R only. The web-research audit found that
practitioners also reject at:

- **Round numbers** (1.1000, 0.0100 multiples) — auto-detected per pair
- **Dynamic MAs** (EMA 50 here)
- **Bollinger band edges** — wick pierces the band, closes back inside

Each rejection type is now a separate source
(`fractal_rejection_top`, `ema_rejection_top`, `bb_rejection_top`,
`round_number_rejection_top`, etc.) so the weight layer can learn
which rejection levels work on which pairs.

## 0-second signal timing

The user requirement: *"একটি এক মিনিটের ক্যান্ডেল যখন 0 সেকেন্ড এ
শুরু হবে, টিক তখনি সিগন্যাল আসতে হবে।"*

The signal pipeline:

1. Closes candle N-1 at the **exact** minute boundary.
2. Computes the full feature pipeline (indicators, patterns,
   reactions, microstructure, mined library) on closed candles only.
3. Decides CALL/PUT (with the per-candle guarantee above).
4. Broadcasts the signal via WebSocket and persists it to the DB,
   with `entry_ts = candle[N-1].ts + 60s` (the open of candle N).
5. `target_close_ts = entry_ts + 60s` (the close of candle N).

Tick polling is at 50ms so the 0-second signal fires within ~50ms of
the candle boundary, suitable for binary-option entries.

## Signal quality: what the numbers mean

A measurement audit ran the real engine over price data with no
predictable edge, where an honest pipeline must score 50%. It scored
33.6% on a thin OTC-style feed — the gap was measurement bugs, not
market reads. What changed:

- **`DRAW` is a real outcome.** When price finishes exactly where it
  started the broker refunds the stake, so it is neither a win nor a
  loss. These used to be recorded as losses; on a thin feed that was
  31% of all graded signals and dragged the measured win rate ~15
  points below reality, which then poisoned every source's learned
  statistics.
- **Fabricated candles are marked and excluded.** When a minute
  passes with no ticks the feed invents a flat candle so the series
  stays continuous. It is a placeholder, not market data:
  `candles.synthetic` marks it, nothing fires a signal on it, nothing
  is graded against it, and the miner doesn't train on it.
- **Every signal carries a `tier`.** `confirmed` passed the quality
  gates; `fallback` is the per-candle-guarantee filler that exists so
  each pair always shows something, and is no better than a coin flip.
  Poll `/api/signals?tier=confirmed` to get only the gated ones. (Rows
  fired before 2026-08-30 may show the old tier name `tier=noise` for
  the same concept.)
- **`confidence` is a measurement or it is `null`.** It's the measured
  hit rate of this signal's sources on this pair, and stays `null`
  until there are at least 25 graded samples.
- **A losing source is demoted, not executed.** A source with a 95%
  interval whose upper bound is still below breakeven gets a weight
  reduction — never a zero weight that would stop it from ever being
  graded again.

## Where a strategy's weight comes from

Every weight used to be a number somebody typed. Indicators got
0.8–1.2, candlestick patterns got 0.10–0.32 (derived from invented
"prior win rates" such as 0.52 for a doji or 0.75 for a candle
reaction), and the two scales were then summed against each other —
so one SMA crossover outvoted ten doji reversals by construction,
whatever either had actually done.

There is now one rule for every source (`app/weights.py`):

- **Unmeasured sources all weigh the same.** A new pair starts with
  an honest tie instead of a hierarchy nobody verified.
- **A measured source weighs what it earned**, from its record *on
  that pair* — a pattern that works on EUR/USD has no claim on
  USD/BDT OTC.
- **The estimate is shrunk toward 50%** by 30 pseudo-trades, so a
  3-for-4 start can't promote anything.
- **No source is ever weighted to zero.** A silent source stops
  appearing on signals, so it stops being graded, and could never
  recover — the trap the old "drop below 42%" rule created.

## Testing a strategy: `/api/backtest`

`/api/patterns` only sees sources that made it onto a fired signal,
in whatever direction the combined vote settled on. `/api/backtest`
replays the stored candles and asks each source in isolation: when
*you alone* said CALL, did the next candle close higher?

```
GET  /api/backtest                      # every pair, plus a cross-pair summary
GET  /api/backtest?pair=EUR/USD&limit=5000
python -m app.backtest --pair "EUR/USD" # same thing from the shell
```

It is read-only — no signals written, no `pattern_stats` touched, and
the miner trains into a local library so a run can't disturb the live
one.

Three guards stand between a number and the word "edge", because
without them the tool invents edges on data that provably has none:

1. **A clustering-aware p-value.** Results arrive in runs, so the
   binomial test — which assumes independent trials — reported p=1e-05
   for a 63% hit rate that was pure coin flips. The p-value comes from
   block sign-flip randomisation instead.
2. **FDR correction across the batch.** Screening ~28 sources at 95%
   confidence produces one or two "significant" results every time.
3. **Consistency across sequential folds**, so one lucky stretch
   can't carry the whole period.

Even so, on driftless random walks about one run in six produced a
source that cleared all three — because everything within one pair
is measured on a single price path, and a path can simply favour a
strategy. **The `across_pairs` summary is the real test**: separate
pairs are separate paths, so a source with an edge on several is
evidence, while a source that shines on exactly one is that same
lucky path in a nicer suit.
