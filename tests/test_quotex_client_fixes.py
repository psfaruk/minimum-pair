"""Regression tests for the orphaned-reconnect-loop leaks in get_client().

Background: get_client() is supposed to be a singleton — one Quotex
client shared by the whole app. Quotex.connect() spawns a background
asyncio task (WebsocketClient.run_forever()) that keeps auto-reconnecting
forever on its own schedule; discarding the Quotex object doesn't cancel
that task, only closing it does. Two distinct leaks were found and both
are covered here:

  1. The current client's check_connect() fails, but a fresh Quotex
     object is built and connected without closing the stale one first.
  2. connect() itself fails (or the grace-period poll afterward never
     sees an authenticated state) — the local `client` is discarded via
     a raised ConnectionError without ever being closed, and since it
     was never assigned to the module-level _client either, it can't be
     reached and closed on a later call.

Every retry through either leaked one more orphaned reconnect loop
(retried by the bootstrap loop every RETRY_BACKOFF_SECONDS, or every
CONNECT_GRACE_SECONDS for leak 2), and enough of them hammering Quotex's
websocket endpoint concurrently got the connection rejected outright
with HTTP 403 — the "live data isn't coming" symptom observed in
production, which persisted even after a fresh session token because
leak 2 was still live. The fix for both: close the client being
abandoned before replacing or discarding it.

Run:  python tests/test_quotex_client_fixes.py
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, quotex_client  # noqa: E402


async def test_stale_client_closed_before_replacement() -> None:
    """A client that has been unhealthy LONGER than the grace window is
    torn down and replaced. (Since the auto-reconnect re-authorization
    fix, a brief disconnect is healed in place — see the transient test
    below — so this path now requires persistent unhealthiness.)"""
    stale = MagicMock()
    stale.check_connect = AsyncMock(return_value=False)
    stale.close = AsyncMock()

    fresh = MagicMock()
    fresh.set_session = MagicMock()
    fresh.connect = AsyncMock(return_value=(True, "ok"))
    fresh.set_account_mode = MagicMock()

    quotex_client._client = stale
    # Simulate unhealthiness that has persisted past the grace window
    quotex_client._client_unhealthy_since = (
        time.monotonic() - quotex_client.UNHEALTHY_REBUILD_AFTER_SECONDS - 1
    )
    old_token = config.QUOTEX_SESSION_TOKEN
    config.QUOTEX_SESSION_TOKEN = "fake-token-for-test"
    try:
        with patch.object(quotex_client, "Quotex", return_value=fresh):
            result = await quotex_client.get_client()
    finally:
        config.QUOTEX_SESSION_TOKEN = old_token
        quotex_client._client = None
        quotex_client._client_unhealthy_since = None

    stale.close.assert_awaited_once()
    assert result is fresh, "get_client() must return the newly connected client"
    print("  quotex_client.stale-client-closed-before-replacement OK")


async def test_grace_period_timeout_closes_orphaned_client() -> None:
    """A second, distinct leak: if connect() reports failure and the
    grace-period poll never sees an authenticated state, the locally
    created client was discarded via the raised ConnectionError without
    ever being closed — its own background reconnect task (spawned
    inside connect()) then keeps retrying forever with nothing left to
    reference it, since it was never assigned to the module-level
    _client either. Must be closed before raising."""
    fresh = MagicMock()
    fresh.set_session = MagicMock()
    fresh.connect = AsyncMock(return_value=(False, "rejected"))
    fresh.check_connect = AsyncMock(return_value=False)  # never authenticates
    fresh.close = AsyncMock()

    quotex_client._client = None
    old_token = config.QUOTEX_SESSION_TOKEN
    old_grace = quotex_client.CONNECT_GRACE_SECONDS
    config.QUOTEX_SESSION_TOKEN = "fake-token-for-test"
    quotex_client.CONNECT_GRACE_SECONDS = 0  # deadline already past -> no real wait
    try:
        with patch.object(quotex_client, "Quotex", return_value=fresh):
            try:
                await quotex_client.get_client()
                assert False, "expected ConnectionError"
            except ConnectionError:
                pass
    finally:
        config.QUOTEX_SESSION_TOKEN = old_token
        quotex_client.CONNECT_GRACE_SECONDS = old_grace
        quotex_client._client = None

    fresh.close.assert_awaited_once()
    print("  quotex_client.grace-period-timeout-closes-client OK")


async def test_connect_exception_closes_orphaned_client() -> None:
    """If connect() itself raises, the client it may have already
    started (and whose background task may already be running) must
    still be closed before the exception propagates."""
    fresh = MagicMock()
    fresh.set_session = MagicMock()
    fresh.connect = AsyncMock(side_effect=RuntimeError("boom"))
    fresh.close = AsyncMock()

    quotex_client._client = None
    old_token = config.QUOTEX_SESSION_TOKEN
    config.QUOTEX_SESSION_TOKEN = "fake-token-for-test"
    try:
        with patch.object(quotex_client, "Quotex", return_value=fresh):
            try:
                await quotex_client.get_client()
                assert False, "expected ConnectionError"
            except ConnectionError:
                pass
    finally:
        config.QUOTEX_SESSION_TOKEN = old_token
        quotex_client._client = None

    fresh.close.assert_awaited_once()
    print("  quotex_client.connect-exception-closes-client OK")


async def test_transient_unhealthy_client_gets_grace() -> None:
    """A brief disconnect (socket dead, pyquotex auto-reconnect in
    flight) must NOT trigger a teardown: closing the client kills the
    very reconnect that would have healed it, and the pair tasks keep
    polling the closed object until the watchdog rebuilds everything
    minutes later. First unhealthy observation returns the same client."""
    transient = MagicMock()
    transient.check_connect = AsyncMock(return_value=False)
    transient.close = AsyncMock()

    quotex_client._client = transient
    quotex_client._client_unhealthy_since = None
    try:
        with patch.object(quotex_client, "Quotex") as quotex_ctor:
            result = await quotex_client.get_client()
            quotex_ctor.assert_not_called()
    finally:
        quotex_client._client = None
        quotex_client._client_unhealthy_since = None

    transient.close.assert_not_awaited()
    assert result is transient, (
        "a transiently-unhealthy client must be returned as-is so its "
        "auto-reconnect can heal the feed"
    )
    print("  quotex_client.transient-unhealthy-grace OK")


async def test_healthy_client_is_reused_without_closing() -> None:
    """The common case — check_connect() succeeds — must be a pure
    passthrough: no close(), no new Quotex() at all."""
    healthy = MagicMock()
    healthy.check_connect = AsyncMock(return_value=True)
    healthy.close = AsyncMock()

    quotex_client._client = healthy
    try:
        with patch.object(quotex_client, "Quotex") as quotex_ctor:
            result = await quotex_client.get_client()
            quotex_ctor.assert_not_called()
    finally:
        quotex_client._client = None

    healthy.close.assert_not_awaited()
    assert result is healthy
    print("  quotex_client.healthy-client-reused OK")


if __name__ == "__main__":
    print("quotex-client-fix tests:")
    asyncio.run(test_stale_client_closed_before_replacement())
    asyncio.run(test_grace_period_timeout_closes_orphaned_client())
    asyncio.run(test_connect_exception_closes_orphaned_client())
    asyncio.run(test_transient_unhealthy_client_gets_grace())
    asyncio.run(test_healthy_client_is_reused_without_closing())
    print("ALL QUOTEX-CLIENT-FIX TESTS PASSED")
