# minimum-pair

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
