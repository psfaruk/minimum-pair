"""Cloudflare managed-challenge solver — policy layer.

The actual browser work (launch Chromium, click the Turnstile, harvest
cf_clearance + __cf_bm + the exact User-Agent) lives in
app/browser_transport.py's BrowserSession. This module owns the POLICY
around it:

* TTL cache — cf_clearance lives ~30min-1h, so repeated handshakes in
  that window reuse the solved cookie instead of relaunching Chromium;
* attempt budget — an unsolvable block must not spin Chromium forever
  (bounded attempts per rolling window);
* enable flag — CF_SOLVE_ENABLED=0 turns every path off (for operators
  who route through a residential QUOTEX_PROXY and want no browser in
  the process);
* status surface — /api/status and /api/diagnose report exactly what the
  solver did last.

Why any of this exists: Quotex fronts every endpoint with Cloudflare's
managed challenge, and datacenter egress IPs (Railway etc.) get HTTP 403
with ``cf-mitigated: challenge`` on both pages and websocket upgrades.
A managed challenge cannot be passed by headers or warm-up cookies — it
requires JavaScript execution in a real browser. Verified live from a
blocked datacenter egress (2026-09): the Turnstile checkbox click clears
it in ~3.5s and yields a domain-wide cf_clearance.
"""
import logging
import time
from typing import Any

from app import config
from app.browser_transport import BrowserTransportUnavailable, browser_session

logger = logging.getLogger(__name__)

# Per-domain solved state: domain -> {"cookie", "user_agent", "solved_at"}
_solved: dict[str, dict[str, Any]] = {}
# domain -> [attempt timestamps] (rolling window)
_attempts: dict[str, list[float]] = {}

_status: dict[str, Any] = {
    "enabled": True,
    "last_result": "not attempted",
    "last_solve_at": None,
    "last_domain": None,
    "last_error": None,
}


def status() -> dict[str, Any]:
    """Diagnostics surface for /api/status + /api/diagnose."""
    fresh = [d for d, s in _solved.items() if _fresh(d)]
    return {
        "enabled": config.CF_SOLVE_ENABLED,
        "last_result": _status["last_result"],
        "last_solve_at": _status["last_solve_at"],
        "last_domain": _status["last_domain"],
        "last_error": _status["last_error"],
        "cached_domains": fresh,
        "browser": browser_session().status,
    }


def _fresh(domain: str) -> bool:
    s = _solved.get(domain)
    if not s:
        return False
    return (time.time() - s["solved_at"]) < config.CF_CLEARANCE_TTL_SECONDS


def _rate_limited(domain: str) -> bool:
    now = time.time()
    window = [t for t in _attempts.get(domain, []) if now - t < config.CF_SOLVE_WINDOW_SECONDS]
    _attempts[domain] = window
    return len(window) >= config.CF_SOLVE_MAX_ATTEMPTS


async def solve(domain: str) -> dict[str, str] | None:
    """Returns {"cookie", "user_agent"} with a fresh (or cached)
    clearance for the domain, or None when solving is disabled, budgeted
    out, or the challenge did not clear.

    The browser egresses through config.QUOTEX_PROXY when one is
    configured — the clearance must be earned from the SAME IP the
    websocket will dial from."""
    _status["enabled"] = config.CF_SOLVE_ENABLED
    if not config.CF_SOLVE_ENABLED:
        _status["last_result"] = "disabled (CF_SOLVE_ENABLED=0)"
        return None

    if _fresh(domain):
        cached = _solved[domain]
        logger.info(
            "Reusing cached cf_clearance for %s (%.0fs old)",
            domain, time.time() - cached["solved_at"],
        )
        return {"cookie": cached["cookie"], "user_agent": cached["user_agent"]}

    if _rate_limited(domain):
        _status["last_result"] = "rate-limited (attempt budget consumed)"
        logger.warning(
            "Challenge solver for %s hit the attempt budget (%d per %ds)",
            domain, config.CF_SOLVE_MAX_ATTEMPTS, config.CF_SOLVE_WINDOW_SECONDS,
        )
        return None

    _attempts.setdefault(domain, []).append(time.time())
    _status["last_domain"] = domain
    _status["last_result"] = "solving…"

    session = browser_session()
    try:
        result = await session.solve_challenge(domain)
    except Exception as e:
        _status["last_result"] = f"failed: {type(e).__name__}"
        _status["last_error"] = str(e)
        logger.exception("Challenge solver failed for %s", domain)
        return None

    if result is None or not result.get("cookie"):
        _status["last_result"] = session.status.get("last_solve_result", "no result")
        _status["last_error"] = session.status.get("last_error")
        return None

    _solved[domain] = {
        "cookie": result["cookie"],
        "user_agent": result.get("user_agent", ""),
        "solved_at": time.time(),
    }
    _status["last_result"] = "solved"
    _status["last_solve_at"] = time.time()
    _status["last_error"] = None
    logger.info(
        "Cloudflare challenge SOLVED for %s (cf_clearance + __cf_bm "
        "harvested, UA pinned)",
        domain,
    )
    return {"cookie": result["cookie"], "user_agent": result.get("user_agent", "")}


async def connect_websocket(url: str) -> Any:
    """Browser-hosted websocket factory: opens the REAL websocket inside
    a page whose origin is the websocket host (the only connection path
    that survives Cloudflare's TLS-fingerprint check — the connection IS
    Chrome's own). Used as QuotexAPI.browser_transport_factory."""
    if not config.CF_SOLVE_ENABLED:
        raise BrowserTransportUnavailable("solver disabled (CF_SOLVE_ENABLED=0)")
    return await browser_session().connect_websocket(url)


async def close() -> None:
    """Shuts the persistent browser down (app shutdown / feed rebuild)."""
    global _solved, _attempts
    _solved = {}
    _attempts = {}
    from app.browser_transport import close_browser_session
    await close_browser_session()
