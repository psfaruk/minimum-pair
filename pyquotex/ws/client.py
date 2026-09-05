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
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.protocol import State

from pyquotex._api._waits import backoff_sleep
from pyquotex.dns_bootstrap import (
    DNSResolutionError,
    ensure_resolvable,
    is_dns_error,
    override_for,
)
from pyquotex.global_value import AuthStatus, WebsocketStatus
from pyquotex.types import ReconnectPolicy

logger = logging.getLogger(__name__)

# Process-wide memory of the last websocket host that produced a working
# session, keyed by the broker's primary site domain ("qxbroker.com").
#
# Why process-wide: every get_client() rebuild constructs a fresh
# QuotexAPI + WebsocketClient, and the app's watchdog rebuilds the whole
# chain when the feed dies. An instance-level "sticky host" would be
# forgotten exactly when it matters — at rebuild time — and the new chain
# would start by dialing the primary host AGAIN, paying one full
# handshake-rejection cycle before rediscovering the fallback that
# worked last time. Remembering it here means: once a deployment has
# found a Cloudflare zone that lets it through, every future connect
# tries that zone FIRST.
_STICKY_WSS_HOST: dict[str, str] = {}


def remember_sticky_host(site_domain: str, ws_host: str) -> None:
    """Records ws_host as the last-known-good endpoint for site_domain."""
    if site_domain and ws_host:
        _STICKY_WSS_HOST[site_domain] = ws_host


def sticky_host(site_domain: str) -> str | None:
    """Returns the last-known-good ws host for site_domain, if any."""
    return _STICKY_WSS_HOST.get(site_domain)


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
        # Host-rotation state: the candidate currently being dialed and
        # the ordered candidate list (sticky host first). Populated by
        # run_forever; current_domain() surfaces it for /api/status.
        self._candidates: list[dict[str, str]] = []
        self._active_candidate: dict[str, str] | None = None

    @property
    def wss(self) -> "WebsocketClient":
        """Returns the low-level WebSocket wrapper (self)."""
        return self

    def current_domain(self) -> str:
        """Site domain of the websocket host currently being dialed.

        Surfaced via /api/status so "which Cloudflare zone am I actually
        using right now" is answerable from outside the container."""
        if self._active_candidate:
            return self._active_candidate.get("domain", "")
        return ""

    @staticmethod
    def _is_handshake_rejection(exc: BaseException) -> bool:
        """True when the failure means THIS endpoint refused the HTTP
        upgrade (Cloudflare 403, wrong path, etc.) rather than the
        connection dropping mid-session. These are host-specific: the
        correct response is to rotate to the next candidate, not to
        back off and retry the same rejected door."""
        if isinstance(exc, InvalidStatus):
            code = getattr(getattr(exc, "response", None), "status_code", None)
            return code is None or int(code) >= 400
        return isinstance(exc, InvalidHandshake)

    # Host-specific failures worth rotating over immediately — an
    # endpoint whose name does not even RESOLVE is exactly as dead to
    # us as one whose handshake is rejected. (DNS failures used to
    # count as generic errors: two full backoff cycles per candidate
    # before rotation, which on a DNS-blocked network turned the
    # fallback chain into a minutes-long crawl that never reached the
    # one resolvable host.)
    @staticmethod
    def _is_host_specific_failure(exc: BaseException) -> bool:
        return WebsocketClient._is_handshake_rejection(exc) or is_dns_error(exc)

    def _order_candidates(self, candidates: list[dict[str, str]]) -> list[dict[str, str]]:
        """Returns the candidates with the sticky (last-known-good) host
        moved to the front, preserving the relative order of the rest."""
        ordered = list(candidates)
        sticky = sticky_host(getattr(self.api, "host", ""))
        if sticky:
            for i, c in enumerate(ordered):
                if c.get("url", "").startswith(f"wss://{sticky}/") or c.get("domain") == sticky:
                    if i != 0:
                        ordered = [ordered[i]] + ordered[:i] + ordered[i + 1:]
                    break
        return ordered

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

        Host rotation: ``candidates`` (list of ``{"url", "domain"}``
        dicts) may be passed to dial several broker websocket endpoints
        in turn. They all front the SAME backend and accept the same
        token — they differ only in which Cloudflare zone fronts them,
        and some zones reject datacenter egress IPs outright (the
        "token is valid but it never connects" 403 loop). A host that
        completes a session is remembered process-wide (sticky) and
        tried first on every future connect; a host that gets its
        handshake rejected twice in a row (or rejected once with an
        HTTP 4xx) is rotated past immediately.
        """
        passed = kwargs.pop("candidates", None)
        candidates = list(passed) if passed else [
            {"url": url, "domain": getattr(self.api, "host", "")}
        ]

        # Sticky host first: the zone that worked last time gets the
        # first shot, before the configured default order.
        ordered = self._order_candidates(candidates)
        self._candidates = ordered

        idx = 0
        attempt = 0
        failures_on_current = 0
        while True:
            candidate = ordered[idx]
            self._active_candidate = candidate
            try:
                await self._connect_once(candidate, extra_headers, ssl)
                if self._closing:
                    return
                attempt = 0  # successful run resets the backoff
                failures_on_current = 0
            except ConnectionClosed as e:
                self._handle_close_exception(e)
                failures_on_current += 1
            except Exception as e:
                logger.error("WebSocket error on %s: %s", candidate.get("domain"), e)
                self.api._on_error(e)
                failures_on_current += 1
                if self._is_host_specific_failure(e):
                    # The endpoint refused the upgrade outright, or this
                    # network cannot even resolve its name — retrying
                    # either immediately is how the old code burned
                    # minutes in the same failure loop. Rotate now.
                    failures_on_current = 2

            if not self.policy.enabled or self._closing:
                return
            if self.policy.max_attempts and attempt >= self.policy.max_attempts:
                logger.error(
                    "WebSocket auto-reconnect giving up after %d attempts",
                    attempt,
                )
                return

            attempt += 1
            if failures_on_current >= 2 and len(ordered) > 1:
                nxt = (idx + 1) % len(ordered)
                logger.info(
                    "WebSocket host %s failed %d time(s) in a row — "
                    "rotating to %s",
                    candidate.get("domain"),
                    failures_on_current,
                    ordered[nxt].get("domain"),
                )
                idx = nxt
                failures_on_current = 0
                # Short pause: the next host is a DIFFERENT Cloudflare
                # zone; the failure we're backing off from doesn't
                # predict anything about it.
                await backoff_sleep(
                    0,
                    base=min(self.policy.base_delay, 1.0),
                    cap=min(self.policy.max_delay, 2.0),
                    jitter=self.policy.jitter,
                )
            else:
                logger.info("WebSocket reconnecting (attempt #%d)", attempt)
                await backoff_sleep(
                    attempt - 1,
                    base=self.policy.base_delay,
                    cap=self.policy.max_delay,
                    jitter=self.policy.jitter,
                )

    async def _connect_once(
            self,
            candidate: dict[str, str],
            extra_headers: dict[str, str] | None,
            ssl: Any,
    ) -> None:
        """One ``connect()`` cycle to ONE candidate endpoint. Returns
        when the connection ends."""
        url = candidate["url"]
        domain = candidate.get("domain") or getattr(self.api, "host", "")
        headers = dict(extra_headers) if extra_headers else {}

        # DNS bootstrap: make sure this host CAN be reached from this
        # network before spending a handshake on it. On DNS-blocking
        # networks this resolves the name via DNS-over-HTTPS and installs
        # a transparent getaddrinfo override; if even DoH cannot answer,
        # the candidate is dead — fail fast (as a DNS error, so the
        # rotation logic moves to the next candidate immediately)
        # instead of hanging in the resolver.
        hostname = url.split("/")[2] if "//" in url else url
        if url.startswith("wss:") or url.startswith("ws:"):
            if not await ensure_resolvable(hostname):
                raise DNSResolutionError(
                    f"[dns] {hostname}: not resolvable on this network "
                    "or via DNS-over-HTTPS"
                )

        # __cf_bm expires (~30min) and is bound to this connection's
        # egress AND zone. Reusing the cookie captured at first connect
        # meant every later reconnect presented a stale one — refresh
        # before each handshake so long-lived sessions keep passing
        # Cloudflare. The refresh is domain-aware: each candidate zone
        # needs its OWN cookie (a qxbroker.com __cf_bm is worthless on
        # quotex.io), so the fallback hosts warm their own domain.
        refresher = getattr(self.api, "refresh_handshake_cookies", None)
        if refresher is not None:
            try:
                fresh = await refresher(domain)
                if fresh:
                    headers["Cookie"] = fresh
                elif domain != getattr(self.api, "host", ""):
                    # Never ship the primary zone's cookies to a different
                    # zone — Cloudflare reads mismatched cookies as a bot
                    # fingerprint. No Cookie header beats a wrong one.
                    headers.pop("Cookie", None)
            except Exception as e:  # never block a reconnect on this
                logger.debug("Handshake cookie refresh skipped: %s", e)

        # Origin/Referer must match the zone of the host actually dialed
        # (ws2.quotex.io is quotex.io's zone, NOT qxbroker.com's).
        if domain:
            headers["Origin"] = f"https://{domain}"
            headers["Referer"] = f"https://{domain}/{self.api.lang}/trade"

        doh_ip = override_for(hostname)
        if doh_ip:
            logger.info(
                "Dialing %s via DNS-bootstrap IP %s (name not resolvable "
                "on this network)", hostname, doh_ip,
            )

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
            logger.info(
                "WebSocket handshake accepted by %s (zone %s)",
                url.split("/")[2] if "//" in url else url,
                domain,
            )
            await self.api._on_open()
            # This endpoint let us through — remember it process-wide so
            # future connects (including full get_client() rebuilds after
            # a watchdog teardown) dial it first.
            remember_sticky_host(getattr(self.api, "host", ""), domain)
            self._open_count += 1
            # EVERY fresh open is a brand-new engine.io session the
            # server considers UNAUTHORIZED until the SSID is presented —
            # on the first open too, not just after reconnects. (The
            # first-open send normally happens in connect(); but when
            # host rotation opens the socket later than connect()'s own
            # wait window, that send never ran — the session then sat
            # open but permanently unauthorized: silent feed,
            # "connecting…" forever. Sending it here on every open makes
            # authorization a property of the SESSION, not of the call
            # that happened to win the race. Duplicates are harmless —
            # the server just re-accepts and re-replies.)
            await self._reauthorize()
            if self._open_count > 1:
                # Reconnect path: replay the subscriptions that were
                # active on the previous socket — AFTER the re-authorization
                # above, so they land on an authorized session, in-order
                # on the same socket.
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
        """Presents the SSID authorization on the freshly opened socket.

        Every open (first or reconnect) creates a new engine.io session
        whose only valid state is "not authenticated" — the server
        delivers no quotes, candles or balance updates until the SSID
        is presented. Duplicated sends (connect() also sends one on the
        fast path) are accepted idempotently by the server."""
        ssid = getattr(self.api.state, "SSID", None)
        if not ssid:
            logger.debug(
                "WebSocket session opened without an SSID — nothing to "
                "authorize yet (login flow will send credentials itself)"
            )
            return
        try:
            await self.api.ssid(ssid)
            logger.info("SSID authorization sent on the fresh session")
        except Exception as e:
            logger.error(
                "Failed to send SSID authorization after open: %s", e
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
