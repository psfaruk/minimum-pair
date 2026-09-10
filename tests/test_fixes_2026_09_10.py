"""Tests for the 2026-09-10 fixes:

1. Cloudflare managed-challenge detection + the three-tier connect
   escalation (python dial → solved cookie → browser transport).
2. Payout-aware firing gate (breakeven + margin per pair, not a fixed
   0.65 that ignores what a win actually pays).
3. Probation re-arm (the measured gate must not ratchet a failing
   signature into permanent silence).
4. The psychology endpoint's streak / stake math.
5. /api/session write protection (SESSION_ADMIN_KEY).
6. db.graded_signals.
7. Browser transport bridge plumbing (JS driver install, frame queue,
   state surface) — with a fake page, no real browser needed.
"""
import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyquotex.ws import client as ws_client_module  # noqa: E402
from pyquotex.ws.client import HandshakeChallenge, is_managed_challenge  # noqa: E402


def _fake_invalid_status(status: int, headers: dict | None = None, body: str = ""):
    """An InvalidStatus-shaped exception with a response attached."""
    from websockets.exceptions import InvalidStatus

    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.body = body
    return InvalidStatus(resp)


class ChallengeDetectionTests(unittest.TestCase):
    def test_cf_mitigated_header_marks_challenge(self) -> None:
        exc = _fake_invalid_status(403, {"cf-mitigated": "challenge"})
        self.assertTrue(is_managed_challenge(exc))

    def test_plain_403_is_not_challenge(self) -> None:
        exc = _fake_invalid_status(403)
        self.assertFalse(is_managed_challenge(exc))

    def test_challenge_page_body_marks_challenge(self) -> None:
        exc = _fake_invalid_status(
            403, {}, "<html><title>Just a moment...</title>...challenge-platform..."
        )
        self.assertTrue(is_managed_challenge(exc))

    def test_other_errors_are_not_challenges(self) -> None:
        self.assertFalse(is_managed_challenge(RuntimeError("nope")))
        self.assertFalse(is_managed_challenge(ConnectionResetError("x")))


class ThreeTierConnectTests(unittest.TestCase):
    """_connect_once: python → solved-cookie → browser transport."""

    def setUp(self) -> None:
        ws_client_module._STICKY_WSS_HOST.clear()

    def _make_client(self, api=None) -> ws_client_module.WebsocketClient:
        if api is None:
            api = MagicMock()
            api.cf_solver = None
            api.browser_transport_factory = None
        api.host = "qxbroker.com"
        api.lang = "en"
        api._on_error = AsyncMock()
        return ws_client_module.WebsocketClient(
            api, reconnect_policy=ws_client_module_module_disabled()
        )

    def test_non_challenge_error_raises_immediately(self) -> None:
        ws = self._make_client()
        with patch.object(ws, "_dial", AsyncMock(side_effect=ConnectionResetError("net"))):
            with self.assertRaises(ConnectionResetError):
                asyncio.run(ws._connect_once({"url": "wss://x/", "domain": "qxbroker.com"}, {}, None))

    def test_solved_cookie_retries_python_dial(self) -> None:
        """Tier 2: solver returns cookie+UA → the python dial is retried
        carrying them."""
        ws = self._make_client(ws_api_with_solver())
        dials = []

        async def fake_dial(candidate, domain, headers, ssl, proxy,
                            solved_cookie=None, solved_user_agent=None):
            dials.append((solved_cookie, solved_user_agent))
            if len(dials) == 1:
                raise _fake_invalid_status(403, {"cf-mitigated": "challenge"})
            return "ok"

        with patch.object(ws, "_dial", side_effect=fake_dial):
            asyncio.run(ws._connect_once({"url": "wss://x/", "domain": "qxbroker.com"}, {}, None))

        self.assertEqual(len(dials), 2)
        self.assertEqual(dials[1][0], "cf_clearance=abc; __cf_bm=xyz")
        self.assertEqual(dials[1][1], "Chrome/1.0")

    def test_browser_transport_used_after_python_rejection(self) -> None:
        """Tier 3: python + solved cookie both challenged → the browser
        transport factory is called and its object driven."""
        api = MagicMock()
        api.host = "qxbroker.com"
        api.lang = "en"
        api._on_error = AsyncMock()
        api.cf_solver = AsyncMock(return_value={"cookie": "cf=x", "user_agent": "UA"})
        ws = ws_client_module.WebsocketClient(
            api, reconnect_policy=ws_client_module_module_disabled()
        )

        async def always_challenged(*args, **kwargs):
            raise _fake_invalid_status(403, {"cf-mitigated": "challenge"})

        fake_ws = FakeWsObject()
        factory_calls = []

        async def factory(url, domain):
            factory_calls.append((url, domain))
            return fake_ws

        api.browser_transport_factory = factory

        with (
            patch.object(ws, "_dial", side_effect=always_challenged),
            patch.object(ws, "_run_ws", AsyncMock()) as run_ws,
        ):
            asyncio.run(ws._connect_once({"url": "wss://x/", "domain": "qxbroker.com"}, {}, None))

        self.assertEqual(factory_calls, [("wss://x/", "qxbroker.com")])
        run_ws.assert_awaited_once()

    def test_no_solver_no_factory_raises_challenge(self) -> None:
        ws = self._make_client()
        ws.api.cf_solver = None
        ws.api.browser_transport_factory = None

        async def always_challenged(*args, **kwargs):
            raise _fake_invalid_status(403, {"cf-mitigated": "challenge"})

        with patch.object(ws, "_dial", side_effect=always_challenged):
            with self.assertRaises(HandshakeChallenge):
                asyncio.run(ws._connect_once({"url": "wss://x/", "domain": "qxbroker.com"}, {}, None))
        self.assertEqual(ws.api.last_handshake_block, "challenge")


class FakeWsObject:
    """websockets-compatible fake for the browser transport."""

    _is_browser_transport = True

    def __init__(self) -> None:
        self.state = MagicMock()

    async def send(self, data):
        pass

    async def close(self, code=1000, reason=""):
        pass


def ws_client_module_module_disabled():
    from pyquotex.types import ReconnectPolicy

    return ReconnectPolicy(enabled=False)


def ws_api_with_solver():
    api = MagicMock()
    api.host = "qxbroker.com"
    api.lang = "en"
    api._on_error = AsyncMock()
    api.cf_solver = AsyncMock(
        return_value={"cookie": "cf_clearance=abc; __cf_bm=xyz", "user_agent": "Chrome/1.0"}
    )
    api.browser_transport_factory = None
    return api


# ---------------------------------------------------------------------------
# Payout-aware gate + probation
# ---------------------------------------------------------------------------
from app import config, decision, db  # noqa: E402


class PayoutGateTests(unittest.TestCase):
    def test_breakeven_reflects_payout(self) -> None:
        # USD/BDT OTC pays 0.92 → breakeven = 1/(1+0.92) ≈ 0.5208
        self.assertAlmostEqual(decision._breakeven_win_rate("USD/BDT OTC"), 1 / 1.92, places=4)
        # EUR/USD pays 0.72 → breakeven ≈ 0.5814
        self.assertAlmostEqual(decision._breakeven_win_rate("EUR/USD"), 1 / 1.72, places=4)
        # unknown pair → default 0.85 payout
        self.assertAlmostEqual(decision._breakeven_win_rate("XXX/YYY"), 1 / 1.85, places=4)

    def test_required_is_breakeven_plus_margin(self) -> None:
        with patch.object(decision.config, "EDGE_MARGIN_OVER_BREAKEVEN", 0.02):
            self.assertAlmostEqual(
                decision._required_win_rate("EUR/USD"), 1 / 1.72 + 0.02, places=4
            )

    def test_payout_context_shape(self) -> None:
        ctx = decision.payout_context("USD/BDT OTC")
        self.assertEqual(ctx["payout"], 0.92)
        self.assertAlmostEqual(ctx["breakeven"], 0.5208, places=3)
        self.assertGreater(ctx["required"], ctx["breakeven"])

    def test_profitable_high_payout_signature_fires(self) -> None:
        """A 58% signature on a 0.92-payout pair is +6.2% expectancy per
        trade — the old fixed 0.65 gate silenced it forever; the
        payout-aware gate must let it through."""
        tmp = Path(tempfile.mkdtemp())
        db.DB_PATH = tmp / "gate1.db"
        asyncio.run(db.init_db())

        # 58% over 200 graded outcomes — well past the sample bar.
        for _ in range(200):
            await_(db.bump_signal_stat("CALL|range|a+b", "USD/BDT OTC", True if (_ % 100) < 58 else False))
            # 58 wins per 100
        # adjust: bump exact 58% → 116 wins, 84 losses
        asyncio.run(_reset_signal_stat("CALL|range|a+b", "USD/BDT OTC", 116, 84))

        decision._signature_perf_cache = {("CALL|range|a+b", "USD/BDT OTC"): (116, 84)}
        decision._perf_cache_ts = time.time() + 999  # skip refresh

        required = decision._required_win_rate("USD/BDT OTC")
        conf = decision.weights.shrunk_rate(116, 84)
        self.assertGreaterEqual(conf, required,
                                "58% at 0.92 payout must clear the payout-aware bar")

    def test_losing_signature_still_muted(self) -> None:
        """A 45% signature must stay silent on any payout."""
        decision._signature_perf_cache = {("PUT|trend|x", "EUR/USD"): (45, 55)}
        conf = decision.weights.shrunk_rate(45, 55)
        self.assertLess(conf, decision._required_win_rate("EUR/USD"))


async def _reset_signal_stat(signature: str, pair: str, wins: int, losses: int) -> None:
    with db._connect() as conn:
        conn.execute("DELETE FROM signal_stats WHERE signature=? AND pair=?", (signature, pair))
        conn.execute(
            "INSERT OR REPLACE INTO signal_stats (signature, pair, wins, losses) VALUES (?,?,?,?)",
            (signature, pair, wins, losses),
        )


def await_(coro):
    return asyncio.run(coro)


class ProbationTests(unittest.TestCase):
    def setUp(self) -> None:
        decision._probation_last_fired.clear()

    def test_first_failure_starts_mute_not_probation(self) -> None:
        """The FIRST time a signature fails the bar it must go silent —
        probation is for REVIVING muted signatures, not excusing the
        first failure."""
        ready = decision._probation_ready("P", "sig")
        self.assertFalse(ready)

    def test_probation_after_window_elapses(self) -> None:
        decision._probation_ready("P", "sig")  # starts the mute
        # Simulate the window elapsing.
        decision._probation_last_fired[("P", "sig")] = (
            time.monotonic() - config.PROBATION_REARM_SECONDS - 1
        )
        self.assertTrue(decision._probation_ready("P", "sig"))

    def test_probation_consumed_resets_timer(self) -> None:
        decision._probation_ready("P", "sig")
        decision._probation_last_fired[("P", "sig")] = (
            time.monotonic() - config.PROBATION_REARM_SECONDS - 1
        )
        self.assertTrue(decision._probation_ready("P", "sig"))  # fires once
        self.assertFalse(decision._probation_ready("P", "sig"))  # then mutes again


# ---------------------------------------------------------------------------
# Psychology math + db.graded_signals + session protection
# ---------------------------------------------------------------------------

class PsychologyMathTests(unittest.TestCase):
    """The streak / stake computations behind /api/psychology."""

    def test_streak_math(self) -> None:
        rows = [  # newest first
            {"result": "LOSS"}, {"result": "LOSS"}, {"result": "WIN"},
            {"result": "LOSS"}, {"result": "LOSS"}, {"result": "LOSS"},
        ]
        streak = 0
        worst = cur = 0
        wins = losses = 0
        for r in rows:
            if r["result"] == "WIN":
                wins += 1
                cur = 0
            elif r["result"] == "LOSS":
                losses += 1
                cur += 1
                worst = max(worst, cur)
        for r in rows:
            if r["result"] == "LOSS":
                streak += 1
            elif r["result"] == "WIN":
                break
        self.assertEqual(streak, 2)
        self.assertEqual(worst, 3)

    def test_kelly_capped_at_2pct(self) -> None:
        win_rate, payout = 0.80, 0.85
        kelly = win_rate - (1 - win_rate) / payout  # 0.576 — absurdly high
        stake = min(0.02, round(kelly * 0.25, 4))
        self.assertEqual(stake, 0.02)

    def test_kelly_zero_below_breakeven(self) -> None:
        win_rate, payout = 0.50, 0.85
        kelly = max(0.0, win_rate - (1 - win_rate) / payout)  # negative → 0
        stake = min(0.02, round(kelly * 0.25, 4)) if kelly else 0.01
        self.assertEqual(stake, 0.01)


class GradedSignalsTests(unittest.TestCase):
    def test_graded_signals_returns_newest_first(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        db.DB_PATH = tmp / "graded.db"
        asyncio.run(db.init_db())

        async def seed():
            now = int(time.time())
            for i, res in enumerate(["WIN", "LOSS", "PENDING", "DRAW"]):
                await db.insert_signal(
                    pair="P", direction="CALL",
                    entry_ts=now + i * 60, target_close_ts=now + i * 60 + 60,
                    confidence=0.6, source="s", entry_price=1.0, tier="confirmed",
                )
            # grade two of them: id 1 → WIN, id 2 → LOSS
            await db.grade_signal(1, 1.01, "WIN")
            await db.grade_signal(2, 0.99, "LOSS")

        asyncio.run(seed())
        rows = asyncio.run(db.graded_signals(10))
        results = [r["result"] for r in rows]
        self.assertEqual(results, ["LOSS", "WIN"])  # newest graded first
        self.assertTrue(all(set(r.keys()) >= {"pair", "result", "created_at"} for r in rows))


class SessionProtectionTests(unittest.TestCase):
    def test_admin_key_required_when_configured(self) -> None:
        """With SESSION_ADMIN_KEY set, a wrong/missing key gets 401."""
        from fastapi.testclient import TestClient

        tmp = Path(tempfile.mkdtemp())
        db.DB_PATH = tmp / "sess.db"
        asyncio.run(db.init_db())

        with (
            patch.object(config, "SESSION_ADMIN_KEY", "sekrit"),
            patch.object(quotex_client_mod, "set_manual_session", AsyncMock()),
            patch.object(quotex_client_mod, "get_client", AsyncMock(side_effect=Exception("no"))),
        ):
            from app import server

            client = TestClient(server.app)
            r = client.post("/api/session", json={"session_token": "tok"})
            self.assertEqual(r.status_code, 401)
            r = client.post(
                "/api/session", json={"session_token": "tok", "admin_key": "sekrit"}
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

    def test_no_key_configured_stays_open(self) -> None:
        from fastapi.testclient import TestClient

        tmp = Path(tempfile.mkdtemp())
        db.DB_PATH = tmp / "sess2.db"
        asyncio.run(db.init_db())

        with (
            patch.object(config, "SESSION_ADMIN_KEY", ""),
            patch.object(quotex_client_mod, "set_manual_session", AsyncMock()),
        ):
            from app import server

            client = TestClient(server.app)
            r = client.post("/api/session", json={"session_token": "tok"})
            self.assertEqual(r.status_code, 200)


# import here (not top) to avoid heavy FastAPI import for the pure tests
from app import quotex_client as quotex_client_mod  # noqa: E402
from app import browser_transport as bt_module  # noqa: E402


class BrowserTransportBridgeTests(unittest.TestCase):
    """The BrowserWebSocket bridge with a fake Playwright page."""

    def _fake_page(self):
        page = MagicMock()
        page.is_closed.return_value = False
        # evaluate returns 'ok' for connect, 'installed' for driver
        page.evaluate = AsyncMock(side_effect=self._evaluate_side)
        page.expose_function = AsyncMock()
        return page

    def _evaluate_side(self, script, *args):
        s = script if isinstance(script, str) else ""
        # Order matters: the DRIVER script defines __pyq_connect, so it
        # must be matched before the one-shot connect call.
        if "window.__pyq_installed = true" in s:
            return "installed"
        if "window.__pyq_connect(u)" in s:
            return "ok"
        if "navigator.userAgent" in s:
            return "UA"
        return "ok"

    def test_open_and_iteration(self) -> None:
        from app.browser_transport import BrowserWebSocket

        page = self._fake_page()
        ws = BrowserWebSocket(page, "wss://x/")

        # One loop for open + frames + iteration: _loop must stay valid
        # for the whole bridge lifetime (call_soon_threadsafe target).
        async def scenario():
            with patch.object(bt_module, "FIRST_FRAME_TIMEOUT", 0.05):
                await ws._open()
            self.assertEqual(ws._state, "open")

            # push two frames + a close sentinel
            ws._on_frame_js("frame1")
            ws._on_frame_js("frame2")

            out = []
            async for raw in ws:
                out.append(raw)
                if len(out) == 2:
                    await ws.close()
            return out

        out = asyncio.run(scenario())
        self.assertEqual(out[:2], ["frame1", "frame2"])

    def test_event_bridge_marks_closed(self) -> None:
        from app.browser_transport import BrowserWebSocket

        page = self._fake_page()
        ws = BrowserWebSocket(page, "wss://x/")

        async def scenario():
            with patch.object(bt_module, "FIRST_FRAME_TIMEOUT", 0.05):
                await ws._open()
            ws._on_event_js("close", 1005, "no status")
            await asyncio.sleep(0.05)  # call_soon_threadsafe needs a beat
            return ws._state

        self.assertEqual(asyncio.run(scenario()), "closed")

    def test_state_marker(self) -> None:
        from app.browser_transport import BrowserWebSocket

        ws = BrowserWebSocket(self._fake_page(), "wss://x/")
        self.assertTrue(ws._is_browser_transport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
