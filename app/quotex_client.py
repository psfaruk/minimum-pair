import logging
import time

from pyquotex.stable_api import Quotex

from app import config, railway_control

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


class LoginBackoffError(ConnectionError):
    """Raised instead of attempting a password login that is still
    inside its Cloudflare-avoiding backoff window."""


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


def _backoff_seconds_for(failure_count: int) -> int:
    """Base delay doubled per consecutive failure, capped at the max."""
    if failure_count <= 0:
        return 0
    doubled = config.LOGIN_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1))
    return int(min(doubled, config.LOGIN_BACKOFF_MAX_SECONDS))


def reset_session_failures() -> None:
    """Called after a fresh token is pasted in via /api/session — give it
    a clean slate instead of inheriting an old failure streak."""
    global _session_failure_count
    _session_failure_count = 0


def _record_connect_failure(use_session: bool) -> None:
    """Books one failed connect attempt against whichever credential path
    it used. A dead token eventually forces a password login; a failed
    password login parks the next attempt behind a growing backoff."""
    global _session_failure_count, _password_failure_count, _login_blocked_until

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
    backoff = _backoff_seconds_for(_password_failure_count)
    _login_blocked_until = time.monotonic() + backoff
    logger.warning(
        "Password login failed %d time(s) in a row — not retrying for %ds "
        "(repeated logins from this IP are what trips the Cloudflare block)",
        _password_failure_count,
        backoff,
    )


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
    global _client, _session_failure_count, _password_failure_count, _login_blocked_until
    if _client is not None and await _client.check_connect():
        return _client

    use_session = auth_mode() == "session_token"

    if not use_session:
        remaining = login_backoff_seconds_remaining()
        if remaining > 0:
            raise LoginBackoffError(
                f"Password login is backed off for another {remaining}s "
                f"after {_password_failure_count} consecutive failures "
                "(avoiding a Cloudflare block on repeated logins)"
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
    except Exception:
        # A raised error (DNS, TLS, Cloudflare closing the connection) is
        # just as much a failed attempt as a False return, and on the
        # password path it has to count toward the backoff too — otherwise
        # the retry loop keeps hitting the login page at full speed.
        _record_connect_failure(use_session)
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
            _record_connect_failure(use_session)
            raise ConnectionError(f"Quotex login failed: {reason}")
        logger.info("Connected after %.1fs grace period", CONNECT_GRACE_SECONDS - (deadline - time.monotonic()))

    if use_session:
        _session_failure_count = 0  # this token still works — clear any earlier failure streak
    else:
        _password_failure_count = 0
        _login_blocked_until = 0.0
        # A password login is the one thing that must not have to happen
        # twice: capture what it produced and push it somewhere that
        # survives this process before doing anything else with the
        # connection.
        if _capture_session(client):
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


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
