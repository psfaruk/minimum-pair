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

# Persisted copies of the pasted session. Railway restarts this container
# on every crash and redeploy, which is precisely when an in-memory token
# would be lost — so the token, cookies and user-agent are written to the
# database and reloaded on boot (see load_persisted_state()).
_STATE_TOKEN = "quotex_session_token"
_STATE_COOKIES = "quotex_session_cookies"
_STATE_USER_AGENT = "quotex_user_agent"


class NoSessionTokenError(ConnectionError):
    """Raised when get_client() is called with no session token configured.
    There is no email/password fallback — the caller MUST surface this to
    the user instead of looping on it."""


def auth_mode() -> str:
    """Token-only auth surface: either a token is set, or the app is
    waiting for one to be pasted via the Settings tab."""
    return "session_token" if config.QUOTEX_SESSION_TOKEN else "no_token"


async def _save_session_state() -> None:
    """Persists the pasted session so a restart reuses the token even when
    Railway's API isn't configured (needs DB_PATH on a volume)."""
    try:
        await db.set_state(
            {
                _STATE_TOKEN: config.QUOTEX_SESSION_TOKEN,
                _STATE_COOKIES: config.QUOTEX_SESSION_COOKIES,
                _STATE_USER_AGENT: config.QUOTEX_USER_AGENT,
            }
        )
    except Exception:
        logger.exception("Could not persist the pasted session to the database")


async def load_persisted_state() -> None:
    """Restores the pasted session on boot.

    Without this, Railway's restart policy loses the token on every
    redeploy and the app would sit waiting for it to be pasted in again
    even though nothing about the token itself has changed."""
    try:
        state = await db.get_state(_STATE_TOKEN, _STATE_COOKIES, _STATE_USER_AGENT)
    except Exception:
        logger.exception("Could not read persisted auth state — starting with a clean slate")
        return

    # Env vars win: a token set directly in the environment is treated as
    # newer than whatever the database remembers.
    if not config.QUOTEX_SESSION_TOKEN and state.get(_STATE_TOKEN):
        config.QUOTEX_SESSION_TOKEN = state[_STATE_TOKEN]
        config.QUOTEX_SESSION_COOKIES = state.get(_STATE_COOKIES, "")
        config.QUOTEX_USER_AGENT = state.get(_STATE_USER_AGENT, "") or config.QUOTEX_USER_AGENT
        logger.info("Restored a session token from the database — no token paste needed to start")


async def get_client() -> Quotex:
    """Returns a connected singleton Quotex client, connecting on first
    use.

    pyquotex's own ReconnectPolicy (enabled by default) handles dropped
    websocket connections and re-subscribes active streams automatically
    — the only path that *this* function guards against is the very
    first connect, or a full reconnect after the singleton has been torn
    down by /api/session.

    Token-only: if no session token is configured, this raises
    NoSessionTokenError immediately. There is no email/password login
    path to fall back to.
    """
    global _client
    if _client is not None and await _client.check_connect():
        return _client

    if not config.QUOTEX_SESSION_TOKEN:
        raise NoSessionTokenError(
            "No session token configured — paste one via the Settings tab "
            "(POST /api/session) to connect."
        )

    if _client is not None:
        # The stale client's own WebsocketClient.run_forever() keeps
        # auto-reconnecting in the background forever, on its own
        # schedule, even though check_connect() above just said it isn't
        # authenticated. Building a fresh Quotex object without closing
        # this one first orphans that loop — every retry through here
        # (the bootstrap loop retries every RETRY_BACKOFF_SECONDS) leaks
        # one more of them. Enough orphaned loops hammering the same WS
        # endpoint concurrently looks like a reconnect storm from
        # Quotex's side and gets the connection rejected outright
        # (observed: "server rejected WebSocket connection: HTTP 403"
        # from dozens of simultaneous reconnect attempts at wildly
        # different attempt counts). Close it before replacing.
        try:
            await _client.close()
        except Exception:
            logger.warning("Failed to close the stale Quotex client before reconnecting", exc_info=True)
        _client = None

    client = Quotex(
        email="",
        password="",
        lang=config.QUOTEX_LANG,
        root_path=str(config.SESSION_ROOT),
    )
    client.set_session(
        user_agent=config.QUOTEX_USER_AGENT or DEFAULT_USER_AGENT,
        cookies=config.QUOTEX_SESSION_COOKIES or None,
        ssid=config.QUOTEX_SESSION_TOKEN,
    )
    logger.info("Using session token (skipping login)")

    try:
        ok, reason = await client.connect()
    except Exception as e:
        raise ConnectionError(f"Quotex connect failed: {type(e).__name__}: {e}") from e

    if not ok:
        # pyquotex's own ReconnectPolicy retries the websocket handshake
        # underneath us — keep polling check_connect() across a real-world
        # budget that spans a couple of those cycles.
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
            raise ConnectionError(f"Quotex connect failed: {reason}")
        logger.info("Connected after grace period")

    client.set_account_mode(config.QUOTEX_ACCOUNT_MODE)
    _client = client
    logger.info("Connected to Quotex (%s account)", config.QUOTEX_ACCOUNT_MODE)
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
    """Takes a token pasted in via /api/session. Persisted like on boot so
    a restart keeps using it."""
    config.QUOTEX_SESSION_TOKEN = token
    config.QUOTEX_SESSION_COOKIES = cookies
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
