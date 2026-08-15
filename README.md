# minimum-pair

## Open public signal API (2026-08)

Anyone can fetch the current CALL/PUT signal for every pair, no auth, no
WebSocket — just an HTTP GET. Per the user requirement: *"call put data
signals যেনো অ্যাপ ইউআরএল ব্যবহার করে, যে কেউ ফিচ করতে পারে। একেবারে
ওপেন থাকবে।"* CORS is open (`Access-Control-Allow-Origin: *`), so
browser-side scripts and bots on other domains can hit these directly.

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
**0-second mark** when a new candle opens (computed from the just-closed
previous candle), and the `target_close_ts` is the close of that running
candle. `expires_in_seconds` tells a bot exactly how long the entry is
still valid for.

## Per-pair per-strategy backtest matrix (2026-08)

The user requirement: *"যত গুলো পেয়ার ও স্ট্রাটেজি ব্যবহার করা হয়েছে,
প্রত্যেকটি কে আলাদা আলাদা পেয়ার এর সাথে ব্যাক টেস্ট করতে হবে।"*
`/api/backtest` now produces, for every (pair, source) cell:

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
*more than one* pair (the only test that survives the lucky-path problem
on synthetic data). Per-pair payouts are configurable in
`config.PAIR_PAYOUT`.

## Expanded candlestick pattern library (2026-08)

The original engine covered 7 patterns. The web-research audit
(`/home/z/my-project/download/research/binary-trading-research.md`,
24 web searches across Investopedia, StockCharts ChartSchool, IG,
TrendSpider, etc.) identified that:

1. **Conflated shapes produce wrong-direction calls.** Hammer vs Hanging
   Man are the same shape with opposite context — the old `_hammer` fired
   "CALL" whenever the shape appeared, even after an uptrend (where it's
   a Hanging Man → PUT). Same for Inverted Hammer vs Shooting Star, and
   for Dragonfly vs Gravestone Doji. Now they're separated and require
   the correct prior-trend context.
2. **Missing high-value patterns.** Investopedia ranks Engulfing +
   3-candle stars + Three Soldiers/Crows as the most reliable reversal
   signals. These are now implemented: Piercing Line, Dark Cloud Cover,
   Morning Star, Evening Star, Three White Soldiers, Three Black Crows,
   Three Inside Up/Down, Three Outside Up/Down, Harami Cross.
3. **Doji sub-types.** A doji is not just "indecision" — Dragonfly,
   Gravestone, and Long-Legged have meaningfully different reversal
   bias depending on prior trend.

The full registry (31 patterns) is exposed at `GET /api/strategies`.

## Expanded candle-reaction family (2026-08)

The original `candle_reaction` source detected wick rejection at fractal
swing S/R only. The web-research audit (Tradeciety "7 Rejection Price
Patterns", JumpStartTrading pin bar guide, FBS) found that practitioners
also reject at:

- **Round numbers** (1.1000, 0.0100 multiples) — auto-detected per pair
- **Dynamic MAs** (EMA 50 here)
- **Bollinger band edges** — wick pierces the band, closes back inside

Each rejection type is now a separate source
(`fractal_rejection_top`, `ema_rejection_top`, `bb_rejection_top`,
`round_number_rejection_top`, etc.) so the weight layer can learn which
rejection levels work on which pairs, instead of one opaque bucket
mixing them all.

## 0-second signal timing (2026-08)

The user requirement: *"একটি এক মিনিটের ক্যান্ডেল যখন 0 সেকেন্ড এ শুরু হবে,
টিক তখনি সিগন্যাল আসতে হবে।"*

The signal pipeline now:

1. Closes candle N-1 at the **exact** minute boundary (was `boundary + 1s`).
2. Computes the full feature pipeline (indicators, patterns, reactions,
   microstructure, mined library) on closed candles only.
3. Decides CALL/PUT/NO-SIGNAL.
4. Broadcasts the signal via WebSocket and persists it to the DB, with
   `entry_ts = candle[N-1].ts + 60s` (the open of candle N).
5. `target_close_ts = entry_ts + 60s` (the close of candle N).

Tick polling tightened from 150ms to 50ms so the 0-second signal fires
within ~50ms of the candle boundary, suitable for binary-option entries.

---

## Quotex login vs. Cloudflare on Railway

Quotex's login page sits behind Cloudflare, and Cloudflare will eventually
block repeated email/password logins coming from Railway's shared egress
IPs. This app defends against that in two layers — see
`app/quotex_client.py` and `app/railway_control.py` for the implementation:

1. **Session-token reuse.** Once logged in (via email/password) once, the
   captured SSID token + cookies are reused on every reconnect, which skips
   the Cloudflare-guarded login form entirely (the websocket authenticates
   directly with the token). Only when the token stops working (or none is
   set yet) does the app fall back to a fresh password login.
2. **Login backoff + auto-persistence.** A failed password login no longer
   retries every ~20s (that's what used to trip the Cloudflare block) — it
   backs off for `LOGIN_BACKOFF_BASE_SECONDS` (default 5 min), doubling on
   each consecutive failure up to `LOGIN_BACKOFF_MAX_SECONDS` (default 1h).
   When a password login *does* succeed, the freshly captured token is
   pushed straight into this service's real Railway environment variables
   (`QUOTEX_SESSION_TOKEN` / `QUOTEX_SESSION_COOKIES` / `QUOTEX_USER_AGENT`)
   via Railway's public API, and (by default) the service is redeployed so
   a clean boot verifies the persisted token works end-to-end. That closes
   the loop: log in once, and every future boot reuses that token instead
   of re-triggering the Cloudflare-guarded login page.

To enable step 2, set `RAILWAY_API_TOKEN` in the service's Railway
variables (Railway dashboard -> Account Settings -> Tokens for an account
token — project-scoped tokens are read-only on the public API and can't
write variables; see `.env.example` for the exact variables and
`RAILWAY_TOKEN_TYPE`). `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and
`RAILWAY_SERVICE_ID` are injected by Railway automatically, so the token is
normally the only thing to add. Without it, the app still works, it just
won't survive a redeploy without logging in again.

Check `/api/status` at any time for `auth_mode`, `password_failure_count`,
and `login_backoff_seconds_remaining` to see exactly where the app is in
this cycle — plus `session_persistence` for whether the last write-back to
Railway succeeded. The same three are shown on the Settings tab.

### Why the backoff is stored in the database

Railway restarts this container on every crash and redeploy, and an
in-memory backoff resets to zero on each one — so the counter was at its
weakest exactly when the app was most likely to hammer the login page.
The failure count, the deadline and the captured token are all written to
SQLite at `DB_PATH`, and reloaded before the first connect. **Point
`DB_PATH` at a mounted volume** (e.g. `/data/candles.db`); on ephemeral
disk the state is lost with the container, and with it the protection.

A stored token also means a restart usually needs no login at all — even
with no Railway API token configured, which is the difference between
"logs in once" and "logs in on every boot".

### Not every failed login means the same thing

The wait is sized by what actually went wrong, because treating all three
alike is how one bad login becomes a blocked IP:

| Failure | Wait | Why |
| --- | --- | --- |
| Transient (network, handshake) | 5 min, doubling to 1h | It'll likely clear on its own |
| Cloudflare block (403/429/1020, challenge page) | 1h, doubling to 6h | Retrying *extends* the block |
| Credentials rejected | 6h | Wrong until a human fixes them — and editing `QUOTEX_EMAIL`/`QUOTEX_PASSWORD` clears the wait immediately |

Classification is deliberately conservative: anything unrecognised counts
as transient, so a misread message can't park logins for hours.

### If Cloudflare has already blocked the IP

Two ways back in, neither of which needs the login page:

1. **Paste a token** from a browser session on the Settings tab
   (`POST /api/session`). It's stored like a captured one, so it survives
   restarts too.
2. **Set `QUOTEX_LOGIN_PROXY`** (e.g. `http://user:pass@host:port`). Only
   the login request goes through it; the websocket keeps using Railway's
   own connection. Once the login succeeds the token is captured,
   persisted, and the proxy stops mattering.

### When a token expires mid-run

An expired token doesn't raise anything — the websocket just stops
delivering ticks, and nothing would ever notice. A watchdog checks every
`CONNECTION_WATCHDOG_SECONDS` (60) and rebuilds the connection when the
socket has dropped or no pair has ticked for `STALE_FEED_SECONDS` (300 —
OTC pairs trade around the clock, so silence everywhere means the
connection, not the market). The rebuild tries the token first and only
falls back to a password login, under the backoff rules above, if the
token really is dead. `/api/status` reports `feed_stale_seconds` and
`reconnects`.

## Signal quality: what the numbers mean

A measurement audit (`docs/signal-diagnosis.html`) ran the real engine over
price data with no predictable edge, where an honest pipeline must score
50%. It scored 33.6% on a thin OTC-style feed — the gap was measurement
bugs, not market reads. What changed:

- **`DRAW` is a real outcome.** When price finishes exactly where it
  started the broker refunds the stake, so it is neither a win nor a
  loss. These used to be recorded as losses; on a thin feed that was 31%
  of all graded signals and dragged the measured win rate ~15 points
  below reality, which then poisoned every source's learned statistics.
- **Fabricated candles are marked and excluded.** When a minute passes
  with no ticks the feed invents a flat candle so the series stays
  continuous. It is a placeholder, not market data: `candles.synthetic`
  marks it, nothing fires a signal on it, nothing is graded against it,
  and the miner doesn't train on it.
- **Every signal carries a `tier`.** `confirmed` passed the quality
  gates; `noise` is the `ALWAYS_SIGNAL` filler that exists so each pair
  always shows something, and is no better than a coin flip. Poll
  `/api/live?tier=confirmed` to get only the gated ones. `age_seconds`
  and `stale` on the same endpoint distinguish a fresh call from a
  stalled pair's last one.
- **`confidence` is a measurement or it is `null`.** It's the measured
  hit rate of this signal's sources on this pair, and stays `null` until
  there are at least 25 graded samples. The old score blended a
  hard-coded 0.55 prior with a vote-agreement number and was
  uncorrelated with actually being right.
- **A losing source is demoted, not executed.** The old rule dropped any
  source below 42% after 15 samples — which a fair coin flip trips 30% of
  the time, and a dropped source never fires again, so it could never
  earn its way back. Now it takes n >= 40 *and* a 95% interval whose
  upper bound is still below breakeven, and the penalty is a weight
  reduction.

An existing database is migrated and repaired on boot: mis-scored ties
become `DRAW`, flat candles are flagged, and `pattern_stats` is recounted
from the corrected history. Nothing is discarded.

These fixes make the reported numbers true; they do not create an edge.
On edge-free data the engine now scores 50.0% — exactly what an honest
pipeline should, and exactly what a strategy with no edge should.

## Where a strategy's weight comes from

Every weight used to be a number somebody typed. Indicators got 0.8–1.2,
candlestick patterns got 0.10–0.32 (derived from invented "prior win
rates" such as 0.52 for a doji or 0.75 for a candle reaction), and the
two scales were then summed against each other — so one SMA crossover
outvoted ten doji reversals by construction, whatever either had actually
done.

There is now one rule for every source (`app/weights.py`):

- **Unmeasured sources all weigh the same.** A new pair starts with an
  honest tie instead of a hierarchy nobody verified.
- **A measured source weighs what it earned**, from its record *on that
  pair* — a pattern that works on EUR/USD has no claim on USD/BDT OTC.
- **The estimate is shrunk toward 50%** by 30 pseudo-trades, so a 3-for-4
  start can't promote anything.
- **No source is ever weighted to zero.** A silent source stops appearing
  on signals, so it stops being graded, and could never recover — the
  trap the old "drop below 42%" rule created.

Expect fewer `confirmed` signals at first: with equal weights the votes
often tie, and a tie honestly means "no confirmation". They come back as
sources accumulate records and separate from each other.

## Testing a strategy: `/api/backtest`

`/api/patterns` only sees sources that made it onto a fired signal, in
whatever direction the combined vote settled on. `/api/backtest` replays
the stored candles and asks each source in isolation: when *you alone*
said CALL, did the next candle close higher?

```
GET  /api/backtest                      # every pair, plus a cross-pair summary
GET  /api/backtest?pair=EUR/USD&limit=5000
python -m app.backtest --pair "EUR/USD" # same thing from the shell
```

It is read-only — no signals written, no `pattern_stats` touched, and the
miner trains into a local library so a run can't disturb the live one.

Three guards stand between a number and the word "edge", because without
them the tool invents edges on data that provably has none:

1. **A clustering-aware p-value.** Results arrive in runs, so the
   binomial test — which assumes independent trials — reported p=1e-05
   for a 63% hit rate that was pure coin flips. The p-value comes from
   block sign-flip randomisation instead.
2. **FDR correction across the batch.** Screening ~28 sources at 95%
   confidence produces one or two "significant" results every time.
3. **Consistency across sequential folds**, so one lucky stretch can't
   carry the whole period.

Even so, on driftless random walks about one run in six produced a source
that cleared all three — because everything within one pair is measured
on a single price path, and a path can simply favour a strategy. **The
`across_pairs` summary is the real test**: separate pairs are separate
paths, so a source with an edge on several is evidence, while a source
that shines on exactly one is that same lucky path in a nicer suit. On
six independent edge-free paths, no source cleared two.
