"""DNS-over-HTTPS bootstrap resolution for the broker's endpoints.

Why this exists
---------------
The deployed app can end up on a network whose DNS resolver simply
cannot (or will not) resolve the broker's hostnames: ISPs that block
trading platforms at the DNS level, broken ``/etc/resolv.conf`` on
WSL/VPS boxes, captive portals, corporate firewalls. Every connect
attempt then dies with ``[Errno -2] Name or service not known`` before
a single packet reaches Quotex — and until now the app reported that as
"Websocket connection rejected.", sending the user hunting for a
Cloudflare/Token problem that does not exist.

What this module does
---------------------
1. **Classifies** DNS-resolution failures precisely (``is_dns_error``)
   so the websocket rotation logic treats them as host-specific and
   moves on immediately, and so error messages can be honest.
2. **Resolves through DNS-over-HTTPS** public JSON APIs that are
   themselves reached BY IP address — so no local DNS is involved at
   all: Cloudflare ``1.1.1.1``, Google ``8.8.8.8`` (Quad9 as a third
   string). If the local resolver refuses ``ws2.qxbroker.com``, the
   answer is fetched over HTTPS from 1.1.1.1 instead.
3. **Installs a process-wide ``socket.getaddrinfo`` override** so every
   connection path (the websockets handshake, the httpx Cloudflare
   warm-up requests) transparently uses the DoH-resolved IP while still
   sending the correct hostname in TLS SNI, the ``Host`` header and
   cookies. TLS certificate verification is untouched — the hostname is
   only redirected at the resolution step, so a DoH answer cannot
   silently redirect traffic to an impostor server without the
   certificate check failing.

Deployment note
---------------
The override hooks ``socket.getaddrinfo``, which the standard asyncio
event loop calls (in its executor) for every resolve. uvicorn's default
``--loop auto`` installs **uvloop**, whose DNS resolution is implemented
in C and bypasses Python's ``socket.getaddrinfo`` entirely — so the
app must run uvicorn with ``--loop asyncio`` (Procfile does) for the
DoH bootstrap to be effective.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DNSResolutionError",
    "is_dns_error",
    "doh_resolve",
    "ensure_resolvable",
    "install_getaddrinfo_patch",
    "override_for",
    "dns_override_map",
    "last_doh_source",
]


class DNSResolutionError(OSError):
    """Raised when a hostname cannot be resolved locally OR via any
    DNS-over-HTTPS provider. Treated as a host-specific failure by the
    websocket rotation logic."""


# --- Classification ----------------------------------------------------------
#
# Every observed spelling of "the resolver could not answer", across
# Linux/macOS/Windows, so one classifier covers them all:
#
#   Linux/glibc :  [Errno -2] Name or service not known
#   Linux/cache :  [Errno -3] Temporary failure in name resolution
#   macOS       :  [Errno 8] nodename nor servname provided, or not known
#   Windows     :  [Errno 11001] getaddrinfo failed
#   Linux IPv6  :  [Errno -5] No address associated with hostname
DNS_ERROR_MARKERS: tuple[str, ...] = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "getaddrinfo failed",
    "no address associated with hostname",
    "name does not resolve",
    "[dns]",  # DNSResolutionError raised by this module
)


def is_dns_error(exc: BaseException | str | None) -> bool:
    """True when *exc* means "the hostname could not be resolved".

    Deliberately broader than ``isinstance(exc, socket.gaierror)``:
    resolvers and wrappers (anyio, httpx, websockets) re-raise DNS
    failures as bare ``OSError`` or embed the resolver's text in their
    own message, and the websocket loop needs to catch all of those.
    """
    if exc is None:
        return False
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, DNSResolutionError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in DNS_ERROR_MARKERS)


# --- DNS-over-HTTPS ----------------------------------------------------------
#
# Providers are reached by IP, never by name — that is the entire point.
# Each entry: (label, url template, extra headers). Cloudflare first
# (fastest, anycast everywhere), Google second, Quad9 third.
_DOH_PROVIDERS: tuple[tuple[str, str, dict[str, str]], ...] = (
    (
        "cloudflare",
        "https://1.1.1.1/dns-query?name={host}&type=A",
        {"accept": "application/dns-json"},
    ),
    (
        "google",
        "https://8.8.8.8/resolve?name={host}&type=A",
        {"accept": "application/dns-json"},
    ),
    (
        "quad9",
        "https://9.9.9.9:5053/dns-query?name={host}&type=A",
        {"accept": "application/dns-json"},
    ),
)

DOH_TIMEOUT_SECONDS = 4.0

_last_doh_source: dict[str, str] = {}


def last_doh_source(hostname: str) -> str:
    """Which provider answered for *hostname* the last time ("" if none)."""
    return _last_doh_source.get(hostname, "")


async def doh_resolve(hostname: str) -> list[str]:
    """Resolves *hostname* to A-record IPs via DNS-over-HTTPS.

    Tries each provider in turn with a short timeout; returns every
    unique IPv4 address found, or ``[]`` when all providers fail (or
    when the domain genuinely does not exist anywhere — the two cases
    are indistinguishable from here, and both mean "cannot connect").
    """
    import httpx  # local import: keeps module import light for tests

    for label, template, headers in _DOH_PROVIDERS:
        url = template.format(host=hostname)
        try:
            async with httpx.AsyncClient() as client:
                resp = await asyncio.wait_for(
                    client.get(url, headers=headers),
                    timeout=DOH_TIMEOUT_SECONDS,
                )
            data = resp.json()
            answers = [
                str(a["data"])
                for a in data.get("Answer", [])
                if a.get("type") == 1 and a.get("data")
            ]
            if answers:
                unique = list(dict.fromkeys(answers))
                _last_doh_source[hostname] = label
                logger.info(
                    "DNS bootstrap: %s resolved via %s DoH -> %s",
                    hostname, label, unique,
                )
                return unique
            # An authoritative NXDOMAIN still comes back HTTP 200 with
            # no Answer — try the next provider before concluding.
            logger.debug("DoH %s: no A answer for %s", label, hostname)
        except Exception as e:
            logger.debug("DoH %s failed for %s: %s", label, hostname, e)
    _last_doh_source[hostname] = ""
    return []


# --- getaddrinfo override ----------------------------------------------------
#
# host -> ip, populated only after a real DoH answer. The patch is
# process-wide and idempotent; every resolver path that eventually goes
# through Python's socket.getaddrinfo (asyncio's default loop, httpx/
# anyio's thread-based resolution) observes it.
_DNS_OVERRIDES: dict[str, str] = {}
_original_getaddrinfo = socket.getaddrinfo
_patch_installed = False


def _patched_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
    ip = _DNS_OVERRIDES.get(str(host))
    if ip:
        return _original_getaddrinfo(ip, *args, **kwargs)
    return _original_getaddrinfo(host, *args, **kwargs)


def install_getaddrinfo_patch() -> None:
    """Installs the override-aware ``socket.getaddrinfo`` (idempotent)."""
    global _patch_installed
    if _patch_installed:
        return
    socket.getaddrinfo = _patched_getaddrinfo
    _patch_installed = True
    logger.debug("DNS bootstrap: getaddrinfo override installed")


def override_for(hostname: str) -> str:
    """The DoH IP currently overriding *hostname* ("" when resolved
    normally)."""
    return _DNS_OVERRIDES.get(hostname, "")


def dns_override_map() -> dict[str, str]:
    """Copy of all active host->IP overrides (for diagnostics)."""
    return dict(_DNS_OVERRIDES)


_LOCAL_RESOLVE_TIMEOUT = 5.0


async def _local_resolves(hostname: str) -> bool:
    """True when the OS resolver answers for *hostname* within a short
    timeout. Runs the blocking resolver in a thread so a hanging
    resolver cannot stall the connect loop."""
    import time as _time

    loop = asyncio.get_running_loop()

    def _resolve() -> bool:
        try:
            infos = _original_getaddrinfo(hostname, 443, socket.AF_INET)
            return bool(infos)
        except Exception:
            return False

    started = _time.monotonic()
    try:
        ok = await asyncio.wait_for(
            loop.run_in_executor(None, _resolve),
            timeout=_LOCAL_RESOLVE_TIMEOUT,
        )
        if ok:
            logger.debug(
                "DNS bootstrap: %s resolved locally in %.2fs",
                hostname, _time.monotonic() - started,
            )
        return ok
    except asyncio.TimeoutError:
        logger.warning(
            "DNS bootstrap: local resolver hung on %s (>%.0fs) — "
            "treating as unresolved", hostname, _LOCAL_RESOLVE_TIMEOUT,
        )
        return False


async def ensure_resolvable(hostname: str) -> bool:
    """Makes sure *hostname* can be connected to, one way or another.

    Order of preference:
      1. already overridden by a previous DoH answer — done;
      2. the OS resolver answers — done (the normal case, zero overhead);
      3. DNS-over-HTTPS answers — the override is installed so every
         later connection (websocket, httpx warm-up) uses that IP while
         presenting the real hostname over TLS; returns True;
      4. nothing anywhere can resolve it — returns False; the caller
         should classify this candidate as dead and rotate.
    """
    if not hostname:
        return False
    if hostname in _DNS_OVERRIDES:
        return True

    if await _local_resolves(hostname):
        return True

    logger.warning(
        "DNS bootstrap: %s does not resolve on this network — "
        "trying DNS-over-HTTPS (1.1.1.1 / 8.8.8.8)", hostname,
    )
    install_getaddrinfo_patch()
    ips = await doh_resolve(hostname)
    if not ips:
        logger.error(
            "DNS bootstrap: %s could not be resolved locally or via any "
            "DoH provider — this network appears to block the broker "
            "entirely", hostname,
        )
        return False
    _DNS_OVERRIDES[hostname] = ips[0]
    return True
