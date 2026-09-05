"""Regression tests for the DNS bootstrap fix (2026-09).

The failure it guards: on networks whose resolver cannot answer the
broker's hostnames (ISP-level DNS blocks on trading platforms, broken
WSL/VPS resolv.conf), every connect attempt died with
``[Errno -2] Name or service not known`` BEFORE any packet reached
Quotex — and the app reported that as "Websocket connection rejected.",
sending the user hunting a phantom Cloudflare/token problem.

The fix: (1) DNS failures are classified and rotate hosts immediately,
(2) unresolvable names are resolved via DNS-over-HTTPS (1.1.1.1 /
8.8.8.8) and the answer is installed as a transparent getaddrinfo
override, (3) error messages state the DNS problem honestly.

Run:  python tests/test_dns_bootstrap.py
"""
import asyncio
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyquotex.dns_bootstrap import (  # noqa: E402
    DNSResolutionError,
    dns_override_map,
    doh_resolve,
    ensure_resolvable,
    install_getaddrinfo_patch,
    is_dns_error,
    override_for,
)
import pyquotex.dns_bootstrap as dns_bootstrap  # noqa: E402
from pyquotex.types import ReconnectPolicy  # noqa: E402
from pyquotex.ws import client as ws_client_module  # noqa: E402
from pyquotex.ws.client import WebsocketClient  # noqa: E402

CANDIDATES = [
    {"url": "wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket", "domain": "qxbroker.com"},
    {"url": "wss://ws2.quotex.io/socket.io/?EIO=3&transport=websocket", "domain": "quotex.io"},
    {"url": "wss://ws2.market-qx.pro/socket.io/?EIO=3&transport=websocket", "domain": "market-qx.pro"},
]


def make_api():
    api = MagicMock()
    api.host = "qxbroker.com"
    api.lang = "en"
    api.state = MagicMock()
    api._on_open = AsyncMock()
    api._on_message = AsyncMock()
    return api


class DNSClassificationTests(unittest.TestCase):
    def test_gaierror_is_dns(self) -> None:
        self.assertTrue(is_dns_error(socket.gaierror(-2, "Name or service not known")))

    def test_linux_glibc_text_is_dns(self) -> None:
        self.assertTrue(is_dns_error(OSError("[Errno -2] Name or service not known")))

    def test_temporary_failure_is_dns(self) -> None:
        self.assertTrue(is_dns_error(OSError("[Errno -3] Temporary failure in name resolution")))

    def test_windows_getaddrinfo_is_dns(self) -> None:
        self.assertTrue(is_dns_error(OSError("[Errno 11001] getaddrinfo failed")))

    def test_module_error_is_dns(self) -> None:
        self.assertTrue(is_dns_error(DNSResolutionError("[dns] ws2.qxbroker.com: not resolvable")))

    def test_rejection_is_not_dns(self) -> None:
        self.assertFalse(is_dns_error("Websocket connection rejected."))
        self.assertFalse(is_dns_error(RuntimeError("server rejected handshake")))

    def test_none_is_not_dns(self) -> None:
        self.assertFalse(is_dns_error(None))
        self.assertFalse(is_dns_error(""))


class RotationOnDNSTests(unittest.TestCase):
    def setUp(self) -> None:
        ws_client_module._STICKY_WSS_HOST.clear()

    def test_dns_failure_rotates_immediately(self) -> None:
        """A DNS failure is host-specific: ONE failure must advance to
        the next candidate (same as a 403), not burn two backoff cycles
        on a name that will not resolve any faster the second time."""
        api = make_api()
        ws = WebsocketClient(api, reconnect_policy=ReconnectPolicy(enabled=True, base_delay=0.01, max_delay=0.02))
        calls = []

        async def fake_connect_once(candidate, headers, ssl):
            calls.append(candidate["domain"])
            if len(calls) < 2:
                raise socket.gaierror(-2, "Name or service not known")
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
        self.assertEqual(calls[1], "quotex.io", "a DNS failure must rotate hosts immediately")

    def test_unresolvable_candidate_raises_dns_error(self) -> None:
        """When ensure_resolvable says a host cannot be resolved, the
        connect attempt fails fast with a DNS-classified error."""
        api = make_api()
        ws = WebsocketClient(api)

        async def fake_ensure(hostname):
            return False

        with patch("pyquotex.ws.client.ensure_resolvable", side_effect=fake_ensure):
            with self.assertRaises(DNSResolutionError):
                asyncio.run(
                    ws._connect_once(CANDIDATES[0], {}, None)
                )


class EnsureResolvableTests(unittest.TestCase):
    def setUp(self) -> None:
        # the REAL override map — dns_override_map() returns a copy, and
        # clearing the copy would leak state across tests
        dns_bootstrap._DNS_OVERRIDES.clear()

    def test_local_resolution_short_circuits(self) -> None:
        """A normally-resolvable host returns True and never installs
        any override."""
        async def run():
            with patch("pyquotex.dns_bootstrap._local_resolves", new=AsyncMock(return_value=True)):
                with patch("pyquotex.dns_bootstrap.doh_resolve", new=AsyncMock()) as doh:
                    ok = await ensure_resolvable("ws2.qxbroker.com")
            return ok, doh.await_count

        ok, doh_calls = asyncio.run(run())
        self.assertTrue(ok)
        self.assertEqual(doh_calls, 0)
        self.assertEqual(override_for("ws2.qxbroker.com"), "")

    def test_doh_answer_installs_override(self) -> None:
        async def run():
            with patch("pyquotex.dns_bootstrap._local_resolves", new=AsyncMock(return_value=False)):
                with patch(
                    "pyquotex.dns_bootstrap.doh_resolve",
                    new=AsyncMock(return_value=["104.18.41.237"]),
                ):
                    return await ensure_resolvable("ws2.qxbroker.com")

        ok = asyncio.run(run())
        self.assertTrue(ok)
        self.assertEqual(override_for("ws2.qxbroker.com"), "104.18.41.237")

    def test_total_failure_returns_false(self) -> None:
        async def run():
            with patch("pyquotex.dns_bootstrap._local_resolves", new=AsyncMock(return_value=False)):
                with patch("pyquotex.dns_bootstrap.doh_resolve", new=AsyncMock(return_value=[])):
                    return await ensure_resolvable("blocked.example.com")

        self.assertFalse(asyncio.run(run()))

    def test_existing_override_short_circuits(self) -> None:
        """Once overridden, later calls must not re-query anything."""
        dns_bootstrap._DNS_OVERRIDES["ws2.quotex.io"] = "104.18.39.143"

        async def run():
            with patch("pyquotex.dns_bootstrap._local_resolves", new=AsyncMock()) as local:
                with patch("pyquotex.dns_bootstrap.doh_resolve", new=AsyncMock()) as doh:
                    ok = await ensure_resolvable("ws2.quotex.io")
            return ok, local.await_count, doh.await_count

        ok, local_calls, doh_calls = asyncio.run(run())
        self.assertTrue(ok)
        self.assertEqual((local_calls, doh_calls), (0, 0))


class GetaddrinfoPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        dns_bootstrap._DNS_OVERRIDES.clear()
        install_getaddrinfo_patch()

    def test_override_redirects_resolution(self) -> None:
        """The patched getaddrinfo must resolve an overridden hostname
        via its DoH IP — by asking for the IP itself."""
        calls = []
        true_original = dns_bootstrap._original_getaddrinfo

        def spy(host, *a, **kw):
            calls.append(host)
            return true_original(host, *a, **kw)

        dns_bootstrap._DNS_OVERRIDES["ws2.qxbroker.com"] = "127.0.0.1"
        with patch.object(dns_bootstrap, "_original_getaddrinfo", side_effect=spy):
            infos = socket.getaddrinfo("ws2.qxbroker.com", 443, socket.AF_INET)

        self.assertEqual(calls, ["127.0.0.1"])
        self.assertTrue(bool(infos))

    def test_unlisted_hosts_pass_through(self) -> None:
        infos = socket.getaddrinfo("localhost", 443, socket.AF_INET)
        self.assertTrue(bool(infos))


class DoHParseTests(unittest.TestCase):
    @staticmethod
    def _client_mock(get_side):
        """An httpx.AsyncClient stand-in supporting `async with` (the
        resolver now closes its clients deterministically). The single
        side_effect item is RETURNED unless it is an exception, which is
        raised."""
        client = MagicMock()
        client.get = AsyncMock(side_effect=[get_side])
        cm = MagicMock()
        cm.__aenter__.return_value = client
        cm.__aexit__.return_value = False
        return cm

    def test_answer_parsing(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            "Status": 0,
            "Answer": [
                {"type": 1, "data": "104.18.41.237"},
                {"type": 1, "data": "172.64.146.19"},
                {"type": 5, "data": "something-else.example"},  # CNAME: ignored
            ],
        }

        async def run():
            with patch("httpx.AsyncClient", return_value=self._client_mock(resp)):
                return await doh_resolve("ws2.qxbroker.com")

        ips = asyncio.run(run())
        self.assertEqual(ips, ["104.18.41.237", "172.64.146.19"])

    def test_all_providers_failing_returns_empty(self) -> None:
        async def run():
            with patch("httpx.AsyncClient", return_value=self._client_mock(RuntimeError("network down"))):
                return await doh_resolve("unreachable.example.com")

        self.assertEqual(asyncio.run(run()), [])


class ErrorMessageTests(unittest.TestCase):
    def test_on_error_reports_dns_honestly(self) -> None:
        """A DNS failure surfaced through _on_error must read as a DNS
        problem — not as 'connection rejected'."""
        from pyquotex.global_value import AuthStatus, WebsocketStatus
        from pyquotex.api import QuotexAPI

        api = QuotexAPI.__new__(QuotexAPI)  # skip __init__; only state is needed
        api.state = MagicMock()
        api.event_registry = MagicMock()
        api.event_registry.set_event = AsyncMock()

        api._on_error(socket.gaierror(-2, "Name or service not known"))
        reason = api.state.websocket_error_reason
        self.assertIn("DNS", reason)
        self.assertNotIn("rejected", reason.lower())

        api._on_error(RuntimeError("some other failure"))
        self.assertEqual(api.state.websocket_error_reason, "some other failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
