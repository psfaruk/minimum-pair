import logging
import time

from pyquotex.stable_api import Quotex

from app import config, db

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

# Persisted copies of the captured session. Railway restarts this container
# on every crash and redeploy, which is precisely when an in-memory token
# would be lost — so the token, cookies and user-agent are written to the
# database and reloaded on boot (see load_persisted_state()).
_STATE_TOKEN = "quotex_session_token"
_STATE_COOKIES = "quotex_session_cookies"
_STATE_USER_AGENT = "quotex_user_agent"

# A single password-login failure now permanently parks further attempts
# until a human pastes a fresh token (or restarts the service after fixing
# the credentials). The previous backoff+doubling retry loop is what
# tripped Cloudflare on shared egress IPs — by design, the app no longer
# retries the Cloudflare-guarded login page on its own.
_password_login_failed = False
_password_failure_reason = ""


class LoginUnavailableError(ConnectionError):
    """Raised when a password login is requested but the app has already
    decided not to attempt one again (a previous attempt failed, or no
    email/password credentials are configured). The caller MUST surface
    this to the user instead of looping."""


def auth_mode() -> str:
    """Which credential path get_client() will use on its next attempt.

    The token path is used whenever a session token is available. The
    password path is only attempted if a token is missing AND no prior
    password login has failed this run — once a password login fails,
    the app stops touching the Cloudflare-guarded login page entirely
    until a human pastes a fresh token.
    """
    if config.QUOTEX_SESSION_TOKEN:
        return "session_token"
    if _password_login_failed:
        return "blocked"
    return "password"


def password_failure_count() -> int:
    """0 until the first failed password login, then 1 forever (within
    this process)."""
    return 1 if _password_login_failed else 0


def login_backoff_seconds_remaining() -> int:
    """Backwards-compatible status field for /api/status and the frontend.

    Returns 0 when a password login is allowed, a very large sentinel
    when one is permanently parked after a failure — so the existing
    Settings UI keeps showing "wait until you paste a token" without
    any retry loop in the backend.
    """
    if not _password_login_failed:
        return 0
    # The frontend formats this as "%dm %ds". Pick a number large enough
    # that the message reads as "until you paste a token" rather than
    # implying a scheduled retry. 24h keeps the UI honest.
    return 24 * 3600


def retry_delay_seconds(default: float) -> float:
    """Kept for backwards compatibility with server._bootstrap_feed().
    A blocked password path should not be retried on a short timer — the
    bootstrap loop falls back to the watchdog's slow cadence instead.
    """
    if _password_login_failed:
        # Long enough that the bootstrap loop effectively stops retrying
        # the password path on its own. The connection watchdog still
        # runs and will pick up a freshly pasted token.
        return max(default, 300.0)
    return default


def last_failure_kind() -> str:
    return _password_failure_reason


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
    """Restores the captured session on boot.

    Without this, Railway's restart policy loses the token on every
    redeploy and the app would have to log in again — which is exactly
    what we want to avoid (a password login is the expensive,
    Cloudflare-guarded step).
    """
    try:
        state = await db.get_state(_STATE_TOKEN, _STATE_COOKIES, _STATE_USER_AGENT)
    except Exception:
        logger.exception("Could not read persisted auth state — starting with a clean slate")
        return

    # Env vars win: a token pasted into Railway (by hand or by a previous
    # run's write-back) is newer than whatever the database remembers.
    if not config.QUOTEX_SESSION_TOKEN and state.get(_STATE_TOKEN):
        config.QUOTEX_SESSION_TOKEN = state[_STATE_TOKEN]
        config.QUOTEX_SESSION_COOKIES = state.get(_STATE_COOKIES, "")
        config.QUOTEX_USER_AGENT = state.get(_STATE_USER_AGENT, "") or config.QUOTEX_USER_AGENT
        logger.info("Restored a session token from the database — no login needed to start")


async def _record_password_failure(reason: str) -> None:
    """Records that a password login has failed. After this, the app
    will not attempt another password login until a fresh token is
    pasted in via /api/session or the service is restarted after the
    credentials are fixed. This is the deliberate end of the retry
    loop: retrying the Cloudflare-guarded login page is what used to
    block the egress IP, so the app no longer does it on its own.
    """
    global _password_login_failed, _password_failure_reason
    _password_login_failed = True
    _password_failure_reason = (reason or "").strip() or "unknown"
    logger.error(
        "Password login failed (%s) — NOT retrying. Paste a fresh session "
        "token via the Settings tab to reconnect; the app will not touch "
        "the login page again until then.",
        _password_failure_reason,
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
    logger.info(
        "Captured session token from password login (%d chars, %d chars of cookies) — future reconnects will reuse it",
        len(token),
        len(config.QUOTEX_SESSION_COOKIES),
    )
    return True


async def get_client() -> Quotex:
    """Returns a connected singleton Quotex client, connecting on first
    use.

    pyquotex's own ReconnectPolicy (enabled by default) handles dropped
    websocket connections and re-subscribes active streams automatically
    — the only path that *this* function guards against is the very
    first connect, or a full reconnect after the singleton has been
    torn down by /api/session.

    The Cloudflare-guarded email/password login is attempted AT MOST
    ONCE per process. If it fails, the app stops touching the login
    page until a human pastes a fresh token. Retrying it is what used
    to block the egress IP.
    """
    global _client
    if _client is not None and await _client.check_connect():
        return _client

    mode = auth_mode()

    if mode == "blocked":
        raise LoginUnavailableError(
            "Password login is unavailable — a previous attempt failed and the "
            "app will not retry. Paste a fresh session token via the Settings "
            "tab (POST /api/session) to reconnect."
        )

    if mode == "password":
        if not (config.QUOTEX_EMAIL and config.QUOTEX_PASSWORD):
            raise ConnectionError(
                "No session token set and no QUOTEX_EMAIL/QUOTEX_PASSWORD configured "
                "— paste a session token via the Settings tab to connect."
            )

    client = Quotex(
        email=config.QUOTEX_EMAIL,
        password=config.QUOTEX_PASSWORD,
        lang=config.QUOTEX_LANG,
        root_path=str(config.SESSION_ROOT),
        # Only the login's HTTP traffic would go through this; the
        # websocket still runs over the service's own connection. It
        # exists so a Cloudflare-blocked egress IP doesn't leave the
        # app with no way back in. With retries removed, the proxy is
        # even more important — it's the one chance a password login
        # has.
        proxies=config.QUOTEX_LOGIN_PROXY or None,
    )

    if mode == "session_token":
        client.set_session(
            user_agent=config.QUOTEX_USER_AGENT or DEFAULT_USER_AGENT,
            cookies=config.QUOTEX_SESSION_COOKIES or None,
            ssid=config.QUOTEX_SESSION_TOKEN,
        )
        logger.info("Using session token (skipping fresh login)")
    else:
        # Quotex() seeds session_data from session.json, which still
        # holds whatever token was last written there. Clearing it is
        # what makes this a *fresh* login.
        client.set_session(
            user_agent=config.QUOTEX_USER_AGENT or DEFAULT_USER_AGENT,
            cookies=None,
            ssid=None,
        )
        logger.info("Attempting email/password login (single attempt — no retry)")

    try:
        ok, reason = await client.connect()
    except Exception as e:
        # A raised error (DNS, TLS, or the HTTP 403 pyquotex raises for
        # a Cloudflare block page) is just as much a failed attempt as a
        # False return. On the password path this is the one and only
        # attempt — record the failure and stop.
        if mode == "password":
            await _record_password_failure(f"{type(e).__name__}: {e}")
        raise

    if not ok:
        # pyquotex's own ReconnectPolicy retries the websocket handshake
        # underneath us — keep polling check_connect() across a real-world
        # budget that spans a couple of those cycles. This is the grace
        # window for the *websocket* part of the connect, NOT a retry
        # of the Cloudflare-guarded login page.
        logger.info(
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
            if mode == "password":
                await _record_password_failure(reason)
            raise ConnectionError(f"Quotex connect failed: {reason}")
        logger.info("Connected after grace period")

    # The token path worked — there is nothing to capture (we already
    # had the token). The password path worked — capture what it
    # produced so the next reconnect uses the token path instead of
    # hitting the login page again.
    if mode == "password" and _capture_session(client):
        await _save_session_state()

    client.set_account_mode(config.QUOTEX_ACCOUNT_MODE)
    _client = client
    logger.info(
        "Connected to Quotex (%s account, auth=%s)",
        config.QUOTEX_ACCOUNT_MODE,
        mode,
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
    one so a restart keeps using it. Pasting a fresh token also clears
    the one-shot password-login failure flag — a human-supplied token
    deserves a clean slate, and is the explicit signal that the
    Cloudflare-guarded login page may be tried again on the next boot
    (if the token later dies and credentials are still configured)."""
    global _password_login_failed, _password_failure_reason
    config.QUOTEX_SESSION_TOKEN = token
    config.QUOTEX_SESSION_COOKIES = cookies
    _password_login_failed = False
    _password_failure_reason = ""
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
