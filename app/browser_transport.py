"""Browser-hosted websocket transport — the Cloudflare-proof connection.

Verified live (2026-09, from a datacenter egress that plain Python could
never reach):

  1. Quotex's pages and websocket endpoints sit behind Cloudflare's
     managed challenge; datacenter IPs get HTTP 403 with
     ``cf-mitigated: challenge`` on everything.
  2. The challenge IS solvable by a real headless Chromium: the Turnstile
     widget's checkbox is clicked via its frame bounding-box, and
     ``cf_clearance`` (domain-wide, ~30min TTL) is issued in ~3.5s.
  3. Python's TLS fingerprint is checked against that clearance: the
     same cookie + UA from httpx/websockets is still 403'd. Only the
     browser that solved the challenge may use it.
  4. A page NAVIGATED to the websocket host (top-level navigation to
     ``https://ws2.qxbroker.com/socket.io/...``) passes Cloudflare and
     lands on a page whose ORIGIN is the websocket host itself.
  5. From that origin page, ``new WebSocket(...)`` — browser TLS, browser
     cookies, correct Origin — connects. (From the site's own origin the
     upgrade is refused; same-origin is what the endpoint wants.)

So the only working datacenter path is: keep a persistent headless
Chromium, solve challenges when they appear, navigate a dedicated page
onto the websocket host, open the real websocket in that page, and
bridge frames both ways:

  JS -> Python   ws.onmessage -> window.__pyq_frame(data)  (expose_function)
  Python -> JS   page.evaluate("window.__pyq_ws.send(...)")

This module implements that bridge with the same interface the
``websockets`` library's connect() returns (send/close/state/async
iteration), so pyquotex's WebsocketClient can drive it transparently.

Everything degrades honestly: no Playwright / no browser binary -> the
transport reports unavailable and the caller falls back to the ordinary
python websocket path (which keeps working on unblocked networks or
through a residential QUOTEX_PROXY).
"""
import asyncio
import logging
import time
from typing import Any

from app import config

logger = logging.getLogger(__name__)

# websockets.protocol.State constants the client checks (imported lazily
# so this module stays importable without websockets installed).
_STATE_OPEN = 1
_STATE_CLOSED = 3


def _state_values() -> tuple[Any, Any]:
    global _STATE_OPEN, _STATE_CLOSED
    try:
        from websockets.protocol import State
        _STATE_OPEN, _STATE_CLOSED = State.OPEN, State.CLOSED
    except Exception:
        pass
    return _STATE_OPEN, _STATE_CLOSED


class BrowserTransportUnavailable(RuntimeError):
    """Raised when the browser transport cannot be established."""


class BrowserSession:
    """One persistent headless Chromium for the whole process.

    Owns the challenge solving (page on the site domain) and the
    transport pages (one per websocket host). Kept alive for the app's
    lifetime; ``close()`` on shutdown.
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._solver_page: Any = None
        self._transport_pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        # diagnostics for /api/status
        self.status: dict[str, Any] = {
            "active": False,
            "last_solve_at": None,
            "last_solve_result": "not attempted",
            "last_error": None,
            "challenges_solved": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def _ensure_browser(self) -> Any:
        if self._closed:
            raise BrowserTransportUnavailable("browser session was closed")
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            self.status["last_error"] = "playwright not installed"
            raise BrowserTransportUnavailable(
                "Playwright is not installed — run: pip install "
                "playwright && python -m playwright install chromium"
            ) from e
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if config.QUOTEX_PROXY:
            launch_kwargs["proxy"] = {"server": config.QUOTEX_PROXY}
        try:
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
        except Exception as e:
            self.status["last_error"] = f"chromium launch failed: {e}"
            raise BrowserTransportUnavailable(
                f"Chromium could not launch (binary or system deps "
                f"missing?): {e}"
            ) from e
        self._context = await self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Dhaka",
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});"
        )
        self.status["active"] = True
        logger.info("Browser transport: Chromium launched (headless)")
        return self._browser

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        self._closed = True
        for page in self._transport_pages.values():
            try:
                await page.close()
            except Exception:
                pass
        self._transport_pages.clear()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._browser = self._context = self._pw = None
        self.status["active"] = False

    # ------------------------------------------------------------------
    # Challenge solving
    # ------------------------------------------------------------------
    async def solve_challenge(self, domain: str) -> dict[str, str] | None:
        """Navigates to the site and clicks the Turnstile until
        cf_clearance exists. Returns {"cookie", "user_agent"} or None.

        Used standalone by pyquotex's cookie solver hook (the python
        websocket path benefits whenever the network lets python TLS
        through, e.g. behind a residential proxy).
        """
        async with self._lock:
            return await self._solve_challenge_locked(domain)

    async def _solve_challenge_locked(self, domain: str) -> dict[str, str] | None:
        try:
            await self._ensure_browser()
        except BrowserTransportUnavailable:
            return None
        page = self._solver_page
        if page is None or page.is_closed():
            page = await self._context.new_page()
            self._solver_page = page
        try:
            await page.goto(
                f"https://{domain}/en",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception:
            logger.debug("goto %s interrupted (challenge redirect)", domain)
        ok = await self._click_turnstile_until_cleared(page, domain)
        if not ok:
            self.status["last_solve_result"] = "challenge did not clear"
            return None
        jar = await self._context.cookies()
        by_name: dict[str, str] = {}
        for c in jar:
            if c["name"] in ("cf_clearance", "__cf_bm") and domain in c["domain"]:
                by_name[c["name"]] = c["value"]
        cookie = "; ".join(f"{k}={v}" for k, v in by_name.items())
        try:
            ua = await page.evaluate("navigator.userAgent")
        except Exception:
            ua = ""
        if not cookie:
            self.status["last_solve_result"] = "cleared but no cookies"
            return None
        self.status["last_solve_at"] = time.time()
        self.status["last_solve_result"] = "solved"
        self.status["challenges_solved"] = self.status.get("challenges_solved", 0) + 1
        return {"cookie": cookie, "user_agent": ua}

    async def _click_turnstile_until_cleared(
        self, page: Any, domain: str, timeout_s: float | None = None
    ) -> bool:
        """Persistent Turnstile clicker (proven live):

        the widget lives in a challenges.cloudflare.com frame that is NOT
        in the light DOM, so the checkbox cannot be selected directly —
        but the frame element's bounding box is available, and the
        checkbox sits ~28px from its left edge, vertically centered.
        Click, wait, re-click (and reload for a fresh challenge) until
        cf_clearance appears.
        """
        timeout_s = timeout_s or config.CF_SOLVE_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout_s
        n = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                cookies = {c["name"] for c in await self._context.cookies()}
            except Exception:
                cookies = set()
            if "cf_clearance" in cookies:
                return True
            n += 1
            if n % 6 == 0:  # ~every 3s
                for f in page.frames:
                    if "challenges.cloudflare.com" not in (f.url or ""):
                        continue
                    try:
                        el = await f.frame_element()
                        box = await el.bounding_box()
                        if box and box["width"] > 20:
                            await page.mouse.click(
                                box["x"] + 28, box["y"] + box["height"] / 2
                            )
                    except Exception:
                        pass
            if n % 30 == 0:  # ~every 15s, reload for a fresh challenge
                try:
                    await page.goto(
                        f"https://{domain}/en",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                except Exception:
                    pass
        return False

    # ------------------------------------------------------------------
    # Transport page + websocket bridge
    # ------------------------------------------------------------------
    async def _transport_page_locked(self, ws_host: str) -> Any:
        """A page whose ORIGIN is the websocket host (required for the
        in-page WS to be accepted). Navigating top-level to the engine.io
        polling URL passes Cloudflare; the page then sits on ws-host
        origin and same-origin websockets connect. Callers hold the lock."""
        page = self._transport_pages.get(ws_host)
        if page is not None and not page.is_closed():
            return page
        page = await self._context.new_page()
        # The first navigation IS the Cloudflare pass-through.
        ok = False
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = await page.goto(
                    f"https://{ws_host}/socket.io/?EIO=3&transport=polling&t=0",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                code = resp.status if resp else None
                if code and code < 400:
                    ok = True
                    break
                # A 403 challenge page here means the clearance expired /
                # was never earned for this zone — solve on the site
                # domain first, then retry the navigation.
            except Exception as e:
                last_err = e
        if not ok:
            # Solve (or re-solve) the challenge on the site domain, then
            # retry the ws-host navigation once more.
            site_domain = ws_host.split(".", 1)[1] if ws_host.count(".") >= 2 else ws_host
            await self._solve_challenge_locked(site_domain)
            try:
                resp = await page.goto(
                    f"https://{ws_host}/socket.io/?EIO=3&transport=polling&t=0",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                ok = bool(resp and resp.status < 400)
            except Exception as e:
                last_err = e
        if not ok:
            await page.close()
            raise BrowserTransportUnavailable(
                f"could not land a page on {ws_host} "
                f"({'last error: ' + str(last_err) if last_err else 'blocked'})"
            )
        self._transport_pages[ws_host] = page
        return page

    async def connect_websocket(self, url: str) -> "BrowserWebSocket":
        """Opens the real websocket inside the ws-host-origin page and
        returns a bridged transport object."""
        async with self._lock:
            await self._ensure_browser()
            ws_host = url.split("/")[2] if "//" in url else url
            page = await self._transport_page_locked(ws_host)
        ws = BrowserWebSocket(page, url)
        await ws._open()
        return ws


# Process-wide singleton
_session: BrowserSession | None = None


def browser_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session


async def close_browser_session() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


# The in-page websocket driver. One instance per transport page; the
# expose_function bindings are installed once and reused across
# reconnections by resetting window.__pyq_ws.
_JS_DRIVER = """
() => {
    if (window.__pyq_installed) return 'reused';
    window.__pyq_installed = true;
    window.__pyq_frames = [];
    window.__pyq_ws = null;
    window.__pyq_state = 'closed';
    window.__pyq_open_promise = null;

    window.__pyq_connect = (url) => {
        window.__pyq_state = 'connecting';
        window.__pyq_frames = [];
        try {
            const ws = new WebSocket(url);
            window.__pyq_ws = ws;
            window.__pyq_state = 'open';
            ws.onmessage = (m) => {
                window.__pyq_frames.push(m.data);
                if (window.__pyq_on_frame) {
                    try { window.__pyq_on_frame(m.data); } catch (e) {}
                }
            };
            ws.onclose = (e) => {
                window.__pyq_state = 'closed';
                if (window.__pyq_on_event) {
                    try { window.__pyq_on_event('close', e.code, String(e.reason || '')); } catch (err) {}
                }
            };
            ws.onerror = () => {
                window.__pyq_state = 'error';
                if (window.__pyq_on_event) {
                    try { window.__pyq_on_event('error', 0, 'websocket error'); } catch (err) {}
                }
            };
            return 'ok';
        } catch (e) {
            window.__pyq_state = 'closed';
            return 'ctor-failed: ' + e;
        }
    };
    window.__pyq_send = (data) => {
        if (window.__pyq_ws && window.__pyq_ws.readyState === 1) {
            window.__pyq_ws.send(data);
            return 'sent';
        }
        return 'not-open';
    };
    window.__pyq_close = () => {
        if (window.__pyq_ws) {
            try { window.__pyq_ws.close(); } catch (e) {}
            window.__pyq_ws = null;
            window.__pyq_state = 'closed';
        }
        return 'closed';
    };
    return 'installed';
}
"""


# How long _open() waits for the server's engine.io handshake before
# declaring the socket dead. Module-level so tests (and deployments with
# slow broker handshakes) can tune it without touching the logic.
FIRST_FRAME_TIMEOUT = 8.0


class BrowserWebSocket:
    """Bridged websocket: the real connection lives in the browser page,
    frames cross the CDP boundary through exposed functions.

    Presents the interface pyquotex's WebsocketClient expects from the
    ``websockets`` library: ``state`` (OPEN/CLOSED), ``send``,
    ``close``, and async iteration over inbound text frames.
    """

    def __init__(self, page: Any, url: str) -> None:
        self._page = page
        self._url = url
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        self._state = "closed"
        self._close_code: int | None = None
        self._close_reason: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bound = False
        # Marker: pyquotex's client uses this to report which transport
        # a connection rides (see WebsocketClient._run_ws).
        self._is_browser_transport = True

    # -- websockets-compatible surface ----------------------------------
    @property
    def state(self) -> Any:
        open_v, closed_v = _state_values()
        return open_v if self._state == "open" else closed_v

    async def send(self, data: str) -> None:
        if self._page.is_closed():
            raise ConnectionError("transport page closed")
        try:
            await self._page.evaluate(
                "(d) => window.__pyq_send(d)", data
            )
        except Exception as e:
            raise ConnectionError(f"browser transport send failed: {e}") from e

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._page.evaluate("() => window.__pyq_close()")
        except Exception:
            pass
        self._state = "closed"
        self._close_code, self._close_reason = code, reason
        # Wake any pending recv() so iteration ends promptly.
        await self._frames.put("")

    async def recv(self) -> str:
        return await self._frames.get()

    def __aiter__(self) -> "BrowserWebSocket":
        return self

    async def __anext__(self) -> str:
        data = await self._frames.get()
        if data == "" and self._state != "open":
            raise StopAsyncIteration
        return data

    async def __aenter__(self) -> "BrowserWebSocket":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # -- bridge plumbing --------------------------------------------------
    async def _open(self) -> None:
        page = self._page
        if page.is_closed():
            raise BrowserTransportUnavailable("transport page closed")

        # The JS callbacks (expose_function) fire on Playwright's thread,
        # possibly a different one than the loop we are on now; capture
        # the running loop so they can enqueue frames thread-safely.
        self._loop = asyncio.get_running_loop()

        if not self._bound:
            await page.expose_function("__pyq_on_frame", self._on_frame_js)
            await page.expose_function("__pyq_on_event", self._on_event_js)
            self._bound = True

        result = await page.evaluate(_JS_DRIVER)
        if result not in ("installed", "reused"):
            raise BrowserTransportUnavailable(f"driver install failed: {result}")

        # Drain any stale frames from a previous connection on this page.
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break

        connect_result = await page.evaluate(
            "(u) => window.__pyq_connect(u)", self._url
        )
        if connect_result != "ok":
            raise BrowserTransportUnavailable(
                f"in-browser WebSocket failed to open: {connect_result}"
            )
        self._state = "open"
        logger.info(
            "Browser transport: websocket OPEN in-page (%s)",
            self._url.split("?")[0],
        )

        # Give the server a moment to send the engine.io handshake; if
        # the socket errored instantly, surface that now instead of
        # letting the caller time out mysteriously.
        try:
            first = await asyncio.wait_for(
                self._frames.get(), timeout=FIRST_FRAME_TIMEOUT
            )
            if first == "" and self._state != "open":
                raise BrowserTransportUnavailable(
                    f"in-browser WebSocket died on open ({self._close_reason})"
                )
            await self._frames.put(first)  # requeue for the reader
        except asyncio.TimeoutError:
            if self._state != "open":
                raise BrowserTransportUnavailable(
                    "in-browser WebSocket closed before the handshake"
                )

    def _on_frame_js(self, data: str) -> None:
        # Called from the Playwright driver (possibly a different thread
        # than our loop) — thread-safe enqueue.
        try:
            self._loop.call_soon_threadsafe(self._frames.put_nowait, data)
        except RuntimeError:
            pass  # loop closed during shutdown

    def _on_event_js(self, kind: str, code: int, reason: str) -> None:
        def _apply() -> None:
            self._state = "closed"
            self._close_code = int(code or 0)
            self._close_reason = reason or kind
            # Sentinel wakes a blocked recv()/iteration so the client's
            # frame loop exits and its reconnect logic runs.
            self._frames.put_nowait("")

        try:
            self._loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            pass
