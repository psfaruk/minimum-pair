import logging
import time

from pyquotex.stable_api import Quotex

from app import config

logger = logging.getLogger(__name__)

_client: Quotex | None = None

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


def auth_mode() -> str:
    """Which credential path get_client() will use on its next attempt."""
    if config.QUOTEX_SESSION_TOKEN and _session_failure_count < config.QUOTEX_SESSION_FAILURE_THRESHOLD:
        return "session_token"
    return "password"


async def get_client() -> Quotex:
    """Returns a connected singleton Quotex client, connecting on first use.

    pyquotex's own ReconnectPolicy (enabled by default) handles dropped
    connections and re-subscribes active streams automatically.
    """
    global _client, _session_failure_count
    if _client is not None and await _client.check_connect():
        return _client

    client = Quotex(
        email=config.QUOTEX_EMAIL,
        password=config.QUOTEX_PASSWORD,
        lang=config.QUOTEX_LANG,
        root_path=str(config.SESSION_ROOT),
    )

    use_session = auth_mode() == "session_token"
    if use_session:
        client.set_session(
            user_agent=config.QUOTEX_USER_AGENT or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            cookies=config.QUOTEX_SESSION_COOKIES or None,
            ssid=config.QUOTEX_SESSION_TOKEN,
        )
        logger.info(
            "Using captured session token (skipping fresh login) — %d/%d prior failures",
            _session_failure_count,
            config.QUOTEX_SESSION_FAILURE_THRESHOLD,
        )
    elif config.QUOTEX_SESSION_TOKEN:
        logger.warning(
            "Session token failed %d times in a row — falling back to email/password login for this attempt",
            _session_failure_count,
        )

    ok, reason = await client.connect()
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
            if use_session:
                _session_failure_count += 1
                if _session_failure_count >= config.QUOTEX_SESSION_FAILURE_THRESHOLD:
                    logger.warning(
                        "Session token has now failed %d times in a row — switching to "
                        "email/password login for future attempts",
                        _session_failure_count,
                    )
            raise ConnectionError(f"Quotex login failed: {reason}")
        logger.info("Connected after %.1fs grace period", CONNECT_GRACE_SECONDS - (deadline - time.monotonic()))

    if use_session:
        _session_failure_count = 0  # this token still works — clear any earlier failure streak

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
