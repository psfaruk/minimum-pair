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
