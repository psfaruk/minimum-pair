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
