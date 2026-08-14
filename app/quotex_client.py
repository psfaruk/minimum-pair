import hashlib
import logging
import time

from pyquotex.stable_api import Quotex

from app import config, db, railway_control

logger = logging.getLogger(__name__)

_client: Quotex | None = None

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# QuotexAPI.connect() sends the SSID auth token and returns as soon as the
# raw websocket is open — it does NOT wait for the server's authorization
# reply, so account.connect() only grants ~2s for that reply before
# reporting "Websocket connection rejected.". On this network that 2s
# window is routinely lost: observed logs show repeated "timed out during
# handshake" errors and the reconnect watchdog cycling roughly every
# 90-150s before a handshake finally completes. pyquotex's own
# ReconnectPolicy (enabled by default) is already retrying underneath us,
# so instead of guessing a timeout, just keep polling check_connect() —
# which itself blocks ~2s per call — across a generous real-world budget
# that spans a couple of those reconnect cycles.
CONNECT_GRACE_SECONDS = 240

# A captured session token (SSID + cookies) skips the Cloudflare-guarded
# login page, but it will eventually expire or get invalidated (cf_clearance
# is typically short-lived; the server can also revoke an SSID). When that
# happens every connect attempt fails the same way regardless of how many
# times or how long we retry — unlike a flaky-network failure, which is
# expected to eventually clear up on its own. So: track *consecutive*
# full-attempt failures while using the session token (each attempt already
# gets its own generous CONNECT_GRACE_SECONDS retry window internally), and
# once that streak crosses the threshold, stop trusting the token and fall
# back to a fresh email/password login instead — automatically, without
# needing a restart or a manually-edited .env.
_session_failure_count = 0

# Password logins are the expensive path: each one loads the
# Cloudflare-guarded sign-in page from Railway's shared egress IPs, and
# retrying that every ~20s is exactly what gets those IPs blocked. So a
# failed password login parks further attempts for a while, doubling the
# wait on each consecutive failure (config.LOGIN_BACKOFF_BASE_SECONDS ->
# LOGIN_BACKOFF_MAX_SECONDS). Session-token connects are not throttled
# this way — they never touch the login page.
_password_failure_count = 0
_login_blocked_until = 0.0  # time.monotonic() deadline; 0 means "free to try"
_last_failure_kind = ""  # why the last password login failed, for /api/status

# Persisted copies of the above. Railway restarts this container on every
# crash and redeploy, which is precisely when an in-memory backoff would
# reset to zero and send a fresh login straight at a Cloudflare block —
# the exact loop this whole module exists to prevent. So the backoff, and
# the captured token itself, are written to the database and reloaded on
# boot (see load_persisted_state()).
_STATE_BLOCKED_UNTIL = "login_blocked_until"
_STATE_FAILURE_COUNT = "login_failure_count"
_STATE_FAILURE_KIND = "login_failure_kind"
_STATE_CREDENTIALS_FINGERPRINT = "login_credentials_fingerprint"
_STATE_TOKEN = "quotex_session_token"
_STATE_COOKIES = "quotex_session_cookies"
_STATE_USER_AGENT = "quotex_user_agent"

# What a failed login is telling us. The three cases need very different
# responses, and treating them all alike is what turns one bad login into
# a blocked IP.
FAILURE_TRANSIENT = "transient"
FAILURE_CLOUDFLARE = "cloudflare_block"
FAILURE_CREDENTIALS = "invalid_credentials"

# Substrings of what Cloudflare's block/challenge pages (and the HTTP
# errors pyquotex raises for them) look like by the time they reach us.
_CLOUDFLARE_MARKERS = (
    "cloudflare",
    "cf-ray",
    "just a moment",
    "checking your browser",
    "attention required",
    "error 1020",
    "access denied",
    "captcha",
    "http 403",
    "http 429",
    "http 503",
)

# Phrases Quotex uses when it has actually rejected the credentials.
# Deliberately narrow: misreading a network hiccup as "bad password"
# would park logins for hours, so anything unrecognised stays transient.
_CREDENTIAL_MARKERS = (
    "do not match our records",
    "does not match our records",
    "incorrect email",
    "incorrect password",
    "invalid email",
    "invalid password",
    "email or password",
    "wrong password",
)


class LoginBackoffError(ConnectionError):
    """Raised instead of attempting a password login that is still
    inside its Cloudflare-avoiding backoff window."""


def _classify_failure(reason: str) -> str:
    text = (reason or "").lower()
    if any(marker in text for marker in _CLOUDFLARE_MARKERS):
        return FAILURE_CLOUDFLARE
    if any(marker in text for marker in _CREDENTIAL_MARKERS):
        return FAILURE_CREDENTIALS
    return FAILURE_TRANSIENT


def _credentials_fingerprint() -> str:
    """Identifies the current credentials without storing them. Lets a
    corrected email/password clear a backoff that was parked because the
    broker rejected the old ones."""
    raw = f"{config.QUOTEX_EMAIL}:{config.QUOTEX_PASSWORD}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def auth_mode() -> str:
    """Which credential path get_client() will use on its next attempt."""
    if config.QUOTEX_SESSION_TOKEN and _session_failure_count < config.QUOTEX_SESSION_FAILURE_THRESHOLD:
        return "session_token"
    return "password"


def password_failure_count() -> int:
    return _password_failure_count


def login_backoff_seconds_remaining() -> int:
    """Seconds left before another password login is allowed (0 if now)."""
    return max(0, int(round(_login_blocked_until - time.monotonic())))


def retry_delay_seconds(default: float) -> float:
    """How long a caller should wait before retrying a failed connect.

    Normally the caller's own short delay, but never shorter than what's
    left of an active login backoff — otherwise the retry loop would just
    walk straight back into the blocked login page.
    """
    return max(default, login_backoff_seconds_remaining())


def last_failure_kind() -> str:
    return _last_failure_kind


def _backoff_seconds_for(failure_count: int, kind: str = FAILURE_TRANSIENT) -> int:
    """How long to wait before the next password login, given how many
    have failed in a row and what the last one actually failed with."""
    if failure_count <= 0:
        return 0
    if kind == FAILURE_CREDENTIALS:
        # Doubling is pointless here — the credentials are wrong until a
        # human changes them, and a changed email/password clears this
        # immediately via the fingerprint check.
        return config.LOGIN_BACKOFF_CREDENTIALS_SECONDS
    if kind == FAILURE_CLOUDFLARE:
        base, cap = config.LOGIN_BACKOFF_CLOUDFLARE_SECONDS, config.LOGIN_BACKOFF_CLOUDFLARE_MAX_SECONDS
    else:
        base, cap = config.LOGIN_BACKOFF_BASE_SECONDS, config.LOGIN_BACKOFF_MAX_SECONDS
    return int(min(base * (2 ** (failure_count - 1)), cap))


async def _save_auth_state() -> None:
    """Persists the backoff so a restart inherits it instead of walking
    straight back into the login page."""
    blocked_for = max(0.0, _login_blocked_until - time.monotonic())
    try:
        await db.set_state(
            {
                _STATE_BLOCKED_UNTIL: str(int(time.time() + blocked_for)),
                _STATE_FAILURE_COUNT: str(_password_failure_count),
                _STATE_FAILURE_KIND: _last_failure_kind,
                _STATE_CREDENTIALS_FINGERPRINT: _credentials_fingerprint(),
            }
        )
    except Exception:
        # Losing the backoff to a storage problem is bad, but taking the
        # app down over it is worse.
        logger.exception("Could not persist login backoff state")


async def _save_session_state() -> None:
    """Persists the captured session so a restart reuses the token even
    when Railway's API isn't configured (needs DB_PATH on a volume)."""
    try:
        await db.set_state(
            {
                _STATE_TOKEN: config.QUOTEX_SESSION_TOKEN,
                _STATE_COOKIES: config.QUOTEX_SESSION_COOKIES,
                _STATE_USER_AGENT: config.QUOTEX_USER_AGENT,
            }
        )
    except Exception:
        logger.exception("Could not persist captured session to the database")


async def load_persisted_state() -> None:
    """Restores the backoff and the last captured session on boot.

    Without this, Railway's restart policy is a backoff-reset button: the
    container dies, comes back with a clean counter, and logs in again
    immediately — which is how a handful of failures becomes a
    Cloudflare block.
    """
    global _password_failure_count, _login_blocked_until, _last_failure_kind

    try:
        state = await db.get_state(
            _STATE_BLOCKED_UNTIL,
            _STATE_FAILURE_COUNT,
            _STATE_FAILURE_KIND,
            _STATE_CREDENTIALS_FINGERPRINT,
            _STATE_TOKEN,
            _STATE_COOKIES,
            _STATE_USER_AGENT,
        )
    except Exception:
        logger.exception("Could not read persisted auth state — starting with a clean slate")
        return

    # Env vars win: a token pasted into Railway (by hand or by this app's
    # own write-back) is newer than whatever the database remembers.
    if not config.QUOTEX_SESSION_TOKEN and state.get(_STATE_TOKEN):
        config.QUOTEX_SESSION_TOKEN = state[_STATE_TOKEN]
        config.QUOTEX_SESSION_COOKIES = state.get(_STATE_COOKIES, "")
        config.QUOTEX_USER_AGENT = state.get(_STATE_USER_AGENT, "") or config.QUOTEX_USER_AGENT
        logger.info("Restored a session token from the database — no login needed to start")

    stored_fingerprint = state.get(_STATE_CREDENTIALS_FINGERPRINT, "")
    if stored_fingerprint and stored_fingerprint != _credentials_fingerprint():
        logger.info("QUOTEX_EMAIL/QUOTEX_PASSWORD changed since the last failure — clearing the login backoff")
        await _save_auth_state()
        return

    _password_failure_count = int(state.get(_STATE_FAILURE_COUNT, "0") or 0)
    _last_failure_kind = state.get(_STATE_FAILURE_KIND, "")
    blocked_until_wall = int(state.get(_STATE_BLOCKED_UNTIL, "0") or 0)
    remaining = blocked_until_wall - time.time()
    if remaining > 0:
        _login_blocked_until = time.monotonic() + remaining
        logger.warning(
            "Restored login backoff from a previous boot: %d consecutive failures (%s), %ds still to wait",
            _password_failure_count,
            _last_failure_kind or "unknown",
            int(remaining),
        )


def reset_session_failures() -> None:
    """Called after a fresh token is pasted in via /api/session — give it
    a clean slate instead of inheriting an old failure streak."""
    global _session_failure_count
    _session_failure_count = 0


async def _record_connect_failure(use_session: bool, reason: str) -> None:
    """Books one failed connect attempt against whichever credential path
    it used. A dead token eventually forces a password login; a failed
    password login parks the next attempt behind a backoff sized by what
    actually went wrong."""
    global _session_failure_count, _password_failure_count, _login_blocked_until, _last_failure_kind

    if use_session:
        _session_failure_count += 1
        if _session_failure_count >= config.QUOTEX_SESSION_FAILURE_THRESHOLD:
            logger.warning(
                "Session token has now failed %d times in a row — switching to "
                "email/password login for future attempts",
                _session_failure_count,
            )
        return

    _password_failure_count += 1
    _last_failure_kind = _classify_failure(reason)
    backoff = _backoff_seconds_for(_password_failure_count, _last_failure_kind)
    _login_blocked_until = time.monotonic() + backoff

    if _last_failure_kind == FAILURE_CLOUDFLARE:
        logger.error(
            "Login was blocked by Cloudflare (failure %d) — waiting %ds before trying again. "
            "Retrying sooner only extends the block; set QUOTEX_LOGIN_PROXY or paste a token "
            "from a browser via the Settings tab to get running immediately.",
            _password_failure_count,
            backoff,
        )
    elif _last_failure_kind == FAILURE_CREDENTIALS:
        logger.error(
            "Quotex rejected the credentials (failure %d) — waiting %ds. Fix QUOTEX_EMAIL / "
            "QUOTEX_PASSWORD; the wait clears as soon as they change.",
            _password_failure_count,
            backoff,
        )
    else:
        logger.warning(
            "Password login failed %d time(s) in a row — not retrying for %ds "
            "(repeated logins from this IP are what trips the Cloudflare block)",
            _password_failure_count,
            backoff,
        )

    await _save_auth_state()


def _capture_session(client: Quotex) -> bool:
    """Copies the SSID token + cookies that a successful password login
    just produced into the running config, so every later reconnect uses
    the token path and skips the login page entirely."""
    data = getattr(client, "session_data", None) or {}
    token = (data.get("token") or "").strip()
    if not token:
        logger.warning("Password login succeeded but no session token was captured — cannot reuse it")
        return False

    config.QUOTEX_SESSION_TOKEN = token
    config.QUOTEX_SESSION_COOKIES = data.get("cookies") or ""
    config.QUOTEX_USER_AGENT = data.get("user_agent") or config.QUOTEX_USER_AGENT
    reset_session_failures()
    logger.info(
        "Captured session token from password login (%d chars, %d chars of cookies) — future reconnects will reuse it",
        len(token),
        len(config.QUOTEX_SESSION_COOKIES),
    )
    return True


async def get_client() -> Quotex:
    """Returns a connected singleton Quotex client, connecting on first use.

    pyquotex's own ReconnectPolicy (enabled by default) handles dropped
    connections and re-subscribes active streams automatically.
    """
    global _client, _session_failure_count, _password_failure_count, _login_blocked_until, _last_failure_kind
    if _client is not None and await _client.check_connect():
        return _client

    use_session = auth_mode() == "session_token"

    if not use_session:
        remaining = login_backoff_seconds_remaining()
        if remaining > 0:
            detail = {
                FAILURE_CLOUDFLARE: "Cloudflare blocked the last login",
                FAILURE_CREDENTIALS: "Quotex rejected these credentials",
            }.get(_last_failure_kind, "avoiding a Cloudflare block on repeated logins")
            raise LoginBackoffError(
                f"Password login is backed off for another {remaining}s "
                f"after {_password_failure_count} consecutive failures ({detail})"
            )
        if not (config.QUOTEX_EMAIL and config.QUOTEX_PASSWORD):
            raise ConnectionError(
                "No usable session token and no QUOTEX_EMAIL/QUOTEX_PASSWORD set — cannot authenticate"
            )

    client = Quotex(
        email=config.QUOTEX_EMAIL,
        password=config.QUOTEX_PASSWORD,
        lang=config.QUOTEX_LANG,
        root_path=str(config.SESSION_ROOT),
        # Only the login's HTTP traffic goes through this; the websocket
        # still runs over the service's own connection. It exists so a
        # Cloudflare-blocked egress IP doesn't leave the app with no way
        # back in.
        proxies=config.QUOTEX_LOGIN_PROXY or None,
    )

    if use_session:
        client.set_session(
            user_agent=config.QUOTEX_USER_AGENT or DEFAULT_USER_AGENT,
            cookies=config.QUOTEX_SESSION_COOKIES or None,
            ssid=config.QUOTEX_SESSION_TOKEN,
        )
        logger.info(
            "Using captured session token (skipping fresh login) — %d/%d prior failures",
            _session_failure_count,
            config.QUOTEX_SESSION_FAILURE_THRESHOLD,
        )
    else:
        # Quotex() seeds session_data from session.json, which still holds
        # whatever token was last written there. Clearing it is what makes
        # this a *fresh* login instead of a silent replay of the same token
        # we've already decided is dead.
        client.set_session(
            user_agent=config.QUOTEX_USER_AGENT or DEFAULT_USER_AGENT,
            cookies=None,
            ssid=None,
        )
        if config.QUOTEX_SESSION_TOKEN:
            logger.warning(
                "Session token failed %d times in a row — falling back to email/password login for this attempt",
                _session_failure_count,
            )
        else:
            logger.info("No session token set — logging in with email/password")

    try:
        ok, reason = await client.connect()
    except Exception as e:
        # A raised error (DNS, TLS, or the HTTP 403 pyquotex raises for a
        # Cloudflare block page) is just as much a failed attempt as a
        # False return, and on the password path it has to count toward
        # the backoff too — otherwise the retry loop keeps hitting the
        # login page at full speed. It also carries the clearest
        # signal of *why*, so it feeds the classifier.
        await _record_connect_failure(use_session, f"{type(e).__name__}: {e}")
        raise

    if not ok:
        logger.warning(
            "Initial connect() reported failure (%s) — pyquotex's "
            "reconnect policy is retrying underneath us, waiting up to "
            "%ds real time for one of those attempts to authenticate "
            "before giving up",
            reason,
            CONNECT_GRACE_SECONDS,
        )
        deadline = time.monotonic() + CONNECT_GRACE_SECONDS
        while time.monotonic() < deadline:
            if await client.check_connect():  # blocks ~2s internally when not yet ready
                ok = True
                break
        if not ok:
            await _record_connect_failure(use_session, reason)
            raise ConnectionError(f"Quotex login failed: {reason}")
        logger.info("Connected after %.1fs grace period", CONNECT_GRACE_SECONDS - (deadline - time.monotonic()))

    if use_session:
        _session_failure_count = 0  # this token still works — clear any earlier failure streak
    else:
        _password_failure_count = 0
        _login_blocked_until = 0.0
        _last_failure_kind = ""
        await _save_auth_state()
        # A password login is the one thing that must not have to happen
        # twice: capture what it produced and store it in both places
        # that outlive this process — the database (instant, works
        # without any Railway credentials) and the service's own Railway
        # variables — before doing anything else with the connection.
        if _capture_session(client):
            await _save_session_state()
            await railway_control.persist_session(
                config.QUOTEX_SESSION_TOKEN,
                config.QUOTEX_SESSION_COOKIES,
                config.QUOTEX_USER_AGENT,
            )

    client.set_account_mode(config.QUOTEX_ACCOUNT_MODE)
    _client = client
    logger.info(
        "Connected to Quotex (%s account, auth=%s)",
        config.QUOTEX_ACCOUNT_MODE,
        "session_token" if use_session else "password",
    )
    return client


async def resolve_asset_codes() -> dict[str, str]:
    """Maps our display names (e.g. 'USD/BDT OTC') to live Quotex asset
    codes, skipping any pair the broker doesn't currently list."""
    client = await get_client()
    available = await client.get_all_assets()

    resolved: dict[str, str] = {}
    for display_name, candidates in config.ALL_PAIRS.items():
        for code in candidates:
            if code in available:
                resolved[display_name] = code
                break
        else:
            logger.warning(
                "No matching Quotex asset code found for %s (tried %s)",
                display_name,
                candidates,
            )
    return resolved


async def set_manual_session(token: str, cookies: str) -> None:
    """Takes a token pasted in via /api/session. Stored like a captured
    one so a restart keeps using it, and it clears the failure streak —
    a human-supplied token deserves a clean slate."""
    config.QUOTEX_SESSION_TOKEN = token
    config.QUOTEX_SESSION_COOKIES = cookies
    reset_session_failures()
    await _save_session_state()


async def is_connected() -> bool:
    """Whether the singleton client is currently authenticated. Used by
    the watchdog — unlike get_client() it never tries to reconnect."""
    if _client is None:
        return False
    try:
        return await _client.check_connect()
    except Exception:
        return False


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
