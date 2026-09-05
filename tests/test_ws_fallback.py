"""Regression tests for the websocket host-rotation fix (2026-09).

The failure it guards: the client had exactly ONE websocket endpoint.
Cloudflare fronts every broker endpoint and some zones reject datacenter
egress IPs outright — so a valid session token still produced an
eternal "connecting…" loop when the single host's upgrade was answered
with 403. The fix dials multiple same-backend endpoints in turn
(rotation), remembers the one that works (sticky, process-wide), and
never ships the primary zone's cookies to another zone.

Run:  python tests/test_ws_fallback.py
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyquotex.types import ReconnectPolicy  # noqa: E402
from pyquotex.ws import client as ws_client_module  # noqa: E402
from pyquotex.ws.client import (  # noqa: E402
    WebsocketClient,
    remember_sticky_host,
    sticky_host,
)

CANDIDATES = [
    {"url": "wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket", "domain": "qxbroker.com"},
    {"url": "wss://ws2.quotex.io/socket.io/?EIO=3&transport=websocket", "domain": "quotex.io"},
    {"url": "wss://ws.quotex.io/socket.io/?EIO=3&transport=websocket", "domain": "quotex.io"},
]


def make_api():
    api = MagicMock()
    api.host = "qxbroker.com"
    api.lang = "en"
    api.state = MagicMock()
    api._on_open = AsyncMock()
    api._on_message = AsyncMock()
    return api


class StickyHostTests(unittest.TestCase):
    def setUp(self) -> None:
        ws_client_module._STICKY_WSS_HOST.clear()

    def test_sticky_host_roundtrip(self) -> None:
        self.assertIsNone(sticky_host("qxbroker.com"))
        remember_sticky_host("qxbroker.com", "ws2.quotex.io")
        self.assertEqual(sticky_host("qxbroker.com"), "ws2.quotex.io")
        # a different site domain has its own entry
        self.assertIsNone(sticky_host("quotex.io"))

    def test_sticky_host_is_dialled_first(self) -> None:
        remember_sticky_host("qxbroker.com", "ws.quotex.io")
        api = make_api()
        ws = WebsocketClient(api)
        ordered = ws._order_candidates(CANDIDATES)
        self.assertEqual(ordered[0]["domain"], "quotex.io")
        self.assertEqual(ordered[0]["url"].startswith("wss://ws.quotex.io/"), True)
        # nothing lost, nothing duplicated
        self.assertEqual(len(ordered), len(CANDIDATES))
        self.assertEqual({c["url"] for c in ordered}, {c["url"] for c in CANDIDATES})

    def test_no_sticky_keeps_default_order(self) -> None:
        api = make_api()
        ws = WebsocketClient(api)
        self.assertEqual(ws._order_candidates(CANDIDATES), CANDIDATES)

    def test_unknown_sticky_host_ignored(self) -> None:
        remember_sticky_host("qxbroker.com", "ws6.some-other-zone.test")
        api = make_api()
        ws = WebsocketClient(api)
        self.assertEqual(ws._order_candidates(CANDIDATES), CANDIDATES)


class RotationTests(unittest.TestCase):
    def setUp(self) -> None:
        ws_client_module._STICKY_WSS_HOST.clear()

    def test_handshake_rejection_classification(self) -> None:
        """HTTP-status upgrade failures are host-specific; mid-session
        drops and unrelated errors are not."""
        from websockets.exceptions import InvalidStatus

        rejection = InvalidStatus(MagicMock(status_code=403))
        self.assertTrue(WebsocketClient._is_handshake_rejection(rejection))
        self.assertFalse(WebsocketClient._is_handshake_rejection(RuntimeError("mid-session error")))

    def test_single_dial_when_reconnect_disabled(self) -> None:
        """With the reconnect policy disabled, run_forever dials the
        first candidate exactly once and returns (the failure is
        reported via api._on_error, not re-raised — the pre-rotation
        behavior this must preserve)."""
        api = make_api()
        ws = WebsocketClient(api, reconnect_policy=ReconnectPolicy(enabled=False))
        calls = []

        async def fake_connect_once(candidate, headers, ssl):
            calls.append(candidate["domain"])
            raise RuntimeError("stop")

        with patch.object(ws, "_connect_once", side_effect=fake_connect_once):
            asyncio.run(ws.run_forever("wss://x/", {}, None, candidates=CANDIDATES))

        self.assertEqual(calls, ["qxbroker.com"])

    def test_rotation_moves_to_next_host_after_two_failures(self) -> None:
        """Two consecutive failures on one host (or one outright 403)
        advance to the next candidate instead of hammering the same
        rejected door."""
        from pyquotex.types import ReconnectPolicy

        api = make_api()
        ws = WebsocketClient(api, reconnect_policy=ReconnectPolicy(enabled=True, base_delay=0.01, max_delay=0.02))
        calls = []

        async def fake_connect_once(candidate, headers, ssl):
            calls.append(candidate["domain"])
            if len(calls) < 3:
                # twice on host 1 → rotation expected for dial #3
                raise ConnectionResetError("network flake")
            await ws.close()
            raise ConnectionResetError("closing now")

        with patch.object(ws, "_connect_once", side_effect=fake_connect_once):
            asyncio.run(
                asyncio.wait_for(
                    ws.run_forever("wss://x/", {}, None, candidates=CANDIDATES),
                    timeout=10,
                )
            )

        self.assertEqual(calls[:3], ["qxbroker.com", "qxbroker.com", "quotex.io"],
                         "after 2 failures on the primary, dialing must rotate to the fallback")

    def test_immediate_rotation_on_http_403(self) -> None:
        """A single outright handshake 403 must rotate at once — this is
        exactly the Cloudflare block that used to loop forever."""
        from websockets.exceptions import InvalidStatus
        from pyquotex.types import ReconnectPolicy

        api = make_api()
        ws = WebsocketClient(api, reconnect_policy=ReconnectPolicy(enabled=True, base_delay=0.01, max_delay=0.02))
        calls = []

        async def fake_connect_once(candidate, headers, ssl):
            calls.append(candidate["domain"])
            if len(calls) < 2:
                raise InvalidStatus(MagicMock(status_code=403))
            await ws.close()
            raise ConnectionResetError("closing now")

        with patch.object(ws, "_connect_once", side_effect=fake_connect_once):
            asyncio.run(
                asyncio.wait_for(
                    ws.run_forever("wss://x/", {}, None, candidates=CANDIDATES),
                    timeout=10,
                )
            )

        self.assertEqual(calls[0], "qxbroker.com")
        self.assertEqual(calls[1], "quotex.io", "a 403 handshake must rotate hosts immediately")

    def test_sticky_host_used_after_remember(self) -> None:
        """Once a host has produced a session, a fresh run_forever starts
        there even though it is not the first candidate."""
        from pyquotex.types import ReconnectPolicy

        remember_sticky_host("qxbroker.com", "ws.quotex.io")
        api = make_api()
        ws = WebsocketClient(api, reconnect_policy=ReconnectPolicy(enabled=True, base_delay=0.01, max_delay=0.02))
        calls = []

        async def fake_connect_once(candidate, headers, ssl):
            calls.append(candidate["domain"])
            await ws.close()
            raise ConnectionResetError("closing now")

        with patch.object(ws, "_connect_once", side_effect=fake_connect_once):
            asyncio.run(
                asyncio.wait_for(
                    ws.run_forever("wss://x/", {}, None, candidates=CANDIDATES),
                    timeout=10,
                )
            )

        self.assertEqual(calls[0], "quotex.io")


class CookieZoneTests(unittest.TestCase):
    def setUp(self) -> None:
        ws_client_module._STICKY_WSS_HOST.clear()

    def test_cookies_never_cross_zones(self) -> None:
        """The primary zone's cookies must not be shipped to a fallback
        zone: Cloudflare reads mismatched cookies as a bot fingerprint."""
        api = make_api()
        api.refresh_handshake_cookies = AsyncMock(return_value="")  # nothing fresh for the fallback
        ws = WebsocketClient(api)

        seen_headers = {}

        class FakeWS:
            state = MagicMock()

            async def send(self, data):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                # endless idle stream; the test cancels the task
                await asyncio.sleep(3600)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=FakeWS())
        ctx.__aexit__ = AsyncMock(return_value=False)

        def fake_ws_connect(url, additional_headers=None, ssl=None, **kw):
            # websockets.connect is a SYNC call returning an object that
            # is then used as an async context manager
            seen_headers.update(additional_headers or {})
            return ctx

        async def scenario():
            with patch.object(ws_client_module.websockets, "connect", side_effect=fake_ws_connect), \
                 patch.object(ws, "_start_watchdog"), patch.object(ws, "_stop_watchdog"):
                task = asyncio.ensure_future(
                    ws._connect_once(CANDIDATES[1], {"Cookie": "primary_cookie=1", "User-Agent": "UA"}, None)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())

        self.assertEqual(seen_headers.get("Origin"), "https://quotex.io")
        self.assertEqual(seen_headers.get("Referer"), "https://quotex.io/en/trade")
        self.assertNotIn("primary_cookie", seen_headers.get("Cookie", ""),
                         "a qxbroker.com cookie must never be sent to quotex.io")

    def test_fresh_cookie_overrides_for_its_domain(self) -> None:
        """When the refresher returns a cookie for the dialed domain, it
        is what gets sent."""
        api = make_api()
        api.refresh_handshake_cookies = AsyncMock(return_value="__cf_bm=fresh")
        ws = WebsocketClient(api)

        seen_headers = {}

        class FakeWS:
            state = MagicMock()

            async def send(self, data):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                # endless idle stream; the test cancels the task
                await asyncio.sleep(3600)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=FakeWS())
        ctx.__aexit__ = AsyncMock(return_value=False)

        def fake_ws_connect(url, additional_headers=None, ssl=None, **kw):
            seen_headers.update(additional_headers or {})
            return ctx

        async def scenario():
            with patch.object(ws_client_module.websockets, "connect", side_effect=fake_ws_connect), \
                 patch.object(ws, "_start_watchdog"), patch.object(ws, "_stop_watchdog"):
                task = asyncio.ensure_future(ws._connect_once(CANDIDATES[0], {}, None))
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())
        self.assertEqual(seen_headers.get("Cookie"), "__cf_bm=fresh")
        self.assertEqual(seen_headers.get("Origin"), "https://qxbroker.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
