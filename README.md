# minimum-pair

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
(`DB_PATH`), so it survives a container restart. The very first
successful password login (if `QUOTEX_EMAIL`/`QUOTEX_PASSWORD` are
configured) also captures and persists the resulting token — after
that, every reconnect uses the token path and never touches the
Cloudflare-guarded login page.

## No retry on a failed password login

Per the user requirement (*"যদি কোনো কারণে প্রথম বারে লগিং ফেইল
হলে, সেকেন্ড টাইম আর লগিং ট্রাই করা যাবে না"*), a failed
password login permanently parks the password path for the rest of
the run. The app does **not** retry the Cloudflare-guarded login page
on its own — retrying that page on a short loop is exactly what used
to block the shared egress IP, so it no longer happens.

| State | Behaviour |
| --- | --- |
| Fresh boot, no token | One password-login attempt is made (if `QUOTEX_EMAIL`/`QUOTEX_PASSWORD` are set) |
| That attempt fails | `auth_mode` flips to `blocked`. The Settings tab shows the failure reason. The app stays alive so a token can be pasted at any time. |
| Token pasted via `/api/session` | `auth_mode` flips to `session_token`, the failure flag clears, the feed reconnects immediately. |
| Mid-run token dies | The connection watchdog notices the silent feed and rebuilds the connection — reusing the same pasted token. It does **not** fall back to a fresh password login. |

`/api/status` exposes `auth_mode`, `password_failure_count`,
`login_backoff_seconds_remaining`, and `last_login_failure` so the
Settings tab can show exactly where the app is in this cycle.

## Per-pair, per-candle signal guarantee

Per the user requirement (*"প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ
সিগন্যাল আসতে হবে"*), every real finalized candle produces a
signal — either `confirmed` (passed the quality gates) or `noise`
(the fallback filler). The previous `if not config.ALWAYS_SIGNAL:
return None` gate in `decision._noise_decision()` would silently
produce nothing on ambiguous candles, breaking that promise; it is
removed, so the per-candle guarantee is absolute.

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

A measurement audit (`docs/signal-diagnosis.html`) ran the real engine
over price data with no predictable edge, where an honest pipeline
must score 50%. It scored 33.6% on a thin OTC-style feed — the gap
was measurement bugs, not market reads. What changed:

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
  gates; `noise` is the `ALWAYS_SIGNAL` filler that exists so each
  pair always shows something, and is no better than a coin flip. Poll
  `/api/signals?tier=confirmed` to get only the gated ones.
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
