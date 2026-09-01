"""Async WebSocket client for the Quotex API.

Resilience layer
----------------
The client supports automatic reconnect with exponential backoff and a
stale-connection watchdog. Both are governed by
:class:`pyquotex.types.ReconnectPolicy`; pass ``ReconnectPolicy(enabled=False)``
to restore the original single-connection behavior.

Reconnect flow:

1. ``run_forever`` enters an outer loop that keeps trying to connect
   until :attr:`ReconnectPolicy.max_attempts` is reached (``0`` = infinite).
2. On every successful open, the :class:`QuotexAPI` ``_on_open`` hook
   runs as before AND a re-subscription pass replays any streams the
   user had opened (candle, all-size, mood, realtime price).
3. On unexpected close or watchdog timeout, the loop sleeps using
   :func:`pyquotex._api._waits.backoff_sleep` and reconnects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from pyquotex._api._waits import backoff_sleep
from pyquotex.global_value import AuthStatus, WebsocketStatus
from pyquotex.types import ReconnectPolicy

logger = logging.getLogger(__name__)


class WebsocketClient:
    """Pure-async WebSocket client with optional auto-reconnect."""

    def __init__(
        self,
        api: Any,
        reconnect_policy: ReconnectPolicy | None = None,
    ) -> None:
        """Initialize the WebSocket client.

        Args:
            api: The :class:`QuotexAPI` instance this client belongs to.
            reconnect_policy: Resilience configuration. Defaults to
                :class:`ReconnectPolicy` with auto-reconnect enabled.
        """
        self.api = api
        self.state = api.state
        self.policy = reconnect_policy or ReconnectPolicy()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._closing = False
        self._watchdog_task: asyncio.Task[None] | None = None
        # Counter of successful opens; the very first open does NOT
        # replay subscriptions (there are none yet).
        self._open_count = 0

    @property
    def wss(self) -> "WebsocketClient":
        """Returns the low-level WebSocket wrapper (self)."""
        return self

    async def send(self, data: str) -> None:
        """Send a frame; log instead of crashing if the socket is closed."""
        if self._ws and self._ws.state is State.OPEN:
            try:
                await self._ws.send(data)
                logger.debug("Sent: %s", data)
            except ConnectionClosed as e:
                logger.warning("Cannot send, connection closed: %s", e)
            except Exception as e:
                logger.error("Error sending WebSocket message: %s", e)

    async def run_forever(
            self,
            url: str,
            extra_headers: dict[str, str] | None = None,
            ssl: Any = None,
            **kwargs: Any,
    ) -> None:
        """Connect to the WebSocket and stay connected.

        With ``ReconnectPolicy.enabled=False`` this method connects once
        and returns when the connection ends. With auto-reconnect on, it
        keeps reconnecting until :meth:`close` is called or
        ``max_attempts`` is exceeded.
        """
        attempt = 0
        while True:
            try:
                await self._connect_once(url, extra_headers, ssl)
                if self._closing:
                    return
                attempt = 0  # successful run resets the backoff
            except ConnectionClosed as e:
                self._handle_close_exception(e)
            except Exception as e:
                logger.error("WebSocket error: %s", e)
                self.api._on_error(e)

            if not self.policy.enabled or self._closing:
                return
            if self.policy.max_attempts and attempt >= self.policy.max_attempts:
                logger.error(
                    "WebSocket auto-reconnect giving up after %d attempts",
                    attempt,
                )
                return

            logger.info("WebSocket reconnecting (attempt #%d)", attempt + 1)
            await backoff_sleep(
                attempt,
                base=self.policy.base_delay,
                cap=self.policy.max_delay,
                jitter=self.policy.jitter,
            )
            attempt += 1

    async def _connect_once(
            self,
            url: str,
            extra_headers: dict[str, str] | None,
            ssl: Any,
    ) -> None:
        """One ``connect()`` cycle. Returns when the connection ends."""
        headers = extra_headers or {}
        async with websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl,
            ping_interval=24,
            ping_timeout=20,
            max_size=2 ** 23,
            compression=None,
        ) as ws:
            self._ws = ws
            self.api.last_message_at = time.monotonic()
            await self.api._on_open()
            self._open_count += 1
            if self._open_count > 1:
                # Reconnect path: this is a brand-new engine.io session and
                # the server considers it UNAUTHORIZED until the SSID is
                # presented again. The old code only replayed subscriptions
                # here — on an unauthorized session those are ignored, so
                # the connection sat open but permanently silent (live data
                # never resumed after any network blip). Re-authorize FIRST
                # so the replayed subscriptions land on an authorized
                # session, in-order on the same socket.
                await self._reauthorize()
                asyncio.create_task(self._replay_subscriptions())

            self._start_watchdog()
            try:
                async for raw in ws:
                    await self.api._on_message(raw)
            finally:
                self._stop_watchdog()

    def _handle_close_exception(self, exc: ConnectionClosed) -> None:
        rcvd = getattr(exc, "rcvd", None)
        sent = getattr(exc, "sent", None)
        if rcvd:
            code, reason = rcvd.code, rcvd.reason
        elif sent:
            code, reason = sent.code, sent.reason
        else:
            code, reason = 1006, str(exc)
        logger.info("WebSocket closed: code=%s, reason=%s", code, reason)
        self.api._on_close(code, reason)

    # ------------------------------------------------------------------
    # Stale-connection watchdog
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        if self.policy.stale_timeout <= 0:
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _stop_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        timeout = self.policy.stale_timeout
        try:
            while self._ws is not None and self._ws.state is State.OPEN:
                await asyncio.sleep(min(timeout / 3.0, 10.0))
                silent_for = time.monotonic() - self.api.last_message_at
                if silent_for > timeout:
                    logger.warning(
                        "WebSocket idle for %.1fs (>%ds); recycling.",
                        silent_for, timeout,
                    )
                    try:
                        await self._ws.close(code=4000, reason="watchdog-stale")
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Re-authorization + subscription replay after reconnect
    # ------------------------------------------------------------------
    async def _reauthorize(self) -> None:
        """Re-send the SSID authorization on a fresh post-reconnect socket.

        Every reconnect creates a new engine.io session whose only valid
        state is "not authenticated" — the server delivers no quotes,
        candles or balance updates until the SSID is presented again."""
        ssid = getattr(self.api.state, "SSID", None)
        if not ssid:
            logger.warning(
                "WebSocket reconnected without an SSID — cannot re-authorize; "
                "this connection will receive no data"
            )
            return
        try:
            await self.api.ssid(ssid)
            logger.info(
                "WebSocket reconnected — SSID authorization re-sent"
            )
        except Exception as e:
            logger.error(
                "Failed to re-send SSID authorization after reconnect: %s", e
            )

    async def _replay_subscriptions(self) -> None:
        """Re-issue every tracked subscription after a successful reconnect.

        Two live-measured constraints shape the timing here:

        1. The server silently DROPS subscription requests that arrive
           while it is still assembling the freshly-authorized session —
           an ``instruments/update`` sent in the same second as
           ``s_authorization`` never activates the quote stream, while
           the identical message sent a few seconds later works
           instantly (``depth/follow`` survives the race; the quote
           stream does not). So the replay waits for the auth to be
           accepted AND for the session-setup burst to settle.
        2. A duplicated subscription is harmless (idempotent server
           side), but a MISSING one is a permanently silent feed — so a
           second replay pass runs a few seconds later as a safety net.
        """
        try:
            for _ in range(40):  # ~2 s: wait for the re-auth to be accepted
                if (
                    self.state.status == WebsocketStatus.CONNECTED
                    and self.state.auth_status == AuthStatus.AUTHENTICATED
                ):
                    break
                await asyncio.sleep(0.05)
        except Exception:  # pragma: no cover
            pass

        await asyncio.sleep(3)  # let the session-setup burst settle

        subs = list(getattr(self.api, "_subscriptions", {}).values())
        for sub in subs:
            try:
                await self._replay_one(sub)
            except Exception as e:
                logger.warning(
                    "Failed to replay subscription kind=%s asset=%s: %s",
                    sub.kind, sub.asset, e,
                )

        # Safety-net second pass: if the first pass landed inside the
        # session-setup window it was dropped silently. Re-send every
        # subscription once more; duplicates are idempotent.
        await asyncio.sleep(5)
        for sub in list(getattr(self.api, "_subscriptions", {}).values()):
            try:
                await self._replay_one(sub)
            except Exception as e:
                logger.warning(
                    "Failed replay retry kind=%s asset=%s: %s",
                    sub.kind, sub.asset, e,
                )

    async def _replay_one(self, sub: Any) -> None:
        api = self.api
        if sub.kind == "candle":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)
            await api.chart_notification(sub.asset)
            await api.follow_candle(sub.asset)
        elif sub.kind == "candle_all_size":
            await api.subscribe_all_size(sub.asset)
        elif sub.kind == "mood":
            instrument = sub.extra.get("instrument", "turbo-option")
            await api.subscribe_Traders_mood(sub.asset, instrument)
        elif sub.kind == "realtime_price":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)

    async def close(self) -> None:
        """Close the websocket gracefully and stop auto-reconnect."""
        self._closing = True
        self.policy = ReconnectPolicy(enabled=False)
        self._stop_watchdog()
        if self._ws and self._ws.state is not State.CLOSED:
            await self._ws.close()

    def is_alive(self) -> bool:
        """Return True iff the underlying socket is currently OPEN."""
        return self._ws is not None and self._ws.state is State.OPEN
