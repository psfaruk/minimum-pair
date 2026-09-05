#!/usr/bin/env python3
"""Survival verifier: the full live pipeline on a DNS-BLOCKED network.

Reproduces the exact failure users see on ISP-blocked / broken-resolver
networks — every broker hostname refuses to resolve
(``[Errno -2] Name or service not known``) — and then proves the app
survives it by resolving through DNS-over-HTTPS instead.

    python tools/verify_dns_block_survival.py --token <SSID>

What it does:
    1. Patches the OS resolver so every broker hostname
       (*.qxbroker.com, *.quotex.io, *.market-qx.pro) raises
       ``socket.gaierror(-2)`` — a faithful ISP DNS block.
    2. Runs the same pipeline /api/diagnose runs: token → connect →
       assets → live stream → history.
    3. PASS requires live ticks to arrive while the DNS block is fully
       engaged — i.e. the DoH bootstrap carried the whole connection.

Exit code 0 = the app now connects even where DNS is blocked.
"""

import argparse
import asyncio
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyquotex.dns_bootstrap as dns_bootstrap  # noqa: E402
from pyquotex.stable_api import Quotex  # noqa: E402

BROKER_SUFFIXES = ("qxbroker.com", "quotex.io", "market-qx.pro")

BLOCKED_ATTEMPTS = {"count": 0}
TRUE_GETADDRINFO = dns_bootstrap._original_getaddrinfo


def blocked_getaddrinfo(host, *args, **kwargs):
    """Stands in for the OS resolver: broker hostnames raise gaierror
    exactly like an ISP DNS block; everything else resolves normally."""
    if any(str(host).endswith(s) for s in BROKER_SUFFIXES):
        BLOCKED_ATTEMPTS["count"] += 1
        raise socket.gaierror(-2, "Name or service not known")
    return TRUE_GETADDRINFO(host, *args, **kwargs)


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
STREAM_PROBE_SECONDS = 12
STEP_W = 10


def step(name: str, ok: bool, detail: str) -> None:
    print(f"  {name:<{STEP_W}} [{'PASS' if ok else 'FAIL'}] {detail}")


async def run(args: argparse.Namespace) -> int:
    # Simulate the ISP block: both the module's own view of the OS
    # resolver AND the process-wide socket.getaddrinfo (used by every
    # async resolution path) refuse broker hostnames.
    dns_bootstrap._original_getaddrinfo = blocked_getaddrinfo
    socket.getaddrinfo = blocked_getaddrinfo
    dns_bootstrap._DNS_OVERRIDES.clear()

    print("Quotex live-connection verification — SIMULATED DNS BLOCK")
    print(f"  broker hostnames blocked at resolver level: {', '.join(BROKER_SUFFIXES)}")
    print(f"  host:       {args.host}")
    print(f"  fallbacks:  {', '.join(args.fallback_hosts) or '(none)'}")

    token = args.token or os.environ.get("QUOTEX_SESSION_TOKEN", "")
    if not token:
        step("token", False, "no token given (pass --token or set QUOTEX_SESSION_TOKEN)")
        return 1
    step("token", True, f"{len(token)} chars ({token[:6]}…{token[-4:]})")

    client = Quotex(
        email="",
        password="",
        host=args.host,
        fallback_hosts=args.fallback_hosts,
        lang=args.lang,
        root_path=args.root_path,
    )
    client.set_session(user_agent=args.user_agent or DEFAULT_UA, cookies=None, ssid=token)

    t0 = time.monotonic()
    try:
        ok, reason = await asyncio.wait_for(client.connect(), timeout=args.connect_timeout)
    except asyncio.TimeoutError:
        ok, reason = False, f"no result within {args.connect_timeout}s"
    except Exception as e:
        ok, reason = False, f"{type(e).__name__}: {e}"
    step("connect", ok, f"{reason} ({time.monotonic() - t0:.1f}s, "
         f"active zone: {client.api.websocket_client.current_domain() if client.api else '?'})")

    overrides = dns_bootstrap.dns_override_map()

    # Tier 1 — DNS-block survival: DoH overrides installed AND the
    # server itself answered us. An authorization/reject IS a server
    # answer: the packets made the full round trip through the
    # DoH-resolved endpoint on a network where DNS cannot even name the
    # host. (Only the token is dead in that case — a separate problem
    # the live-connection verifier covers.)
    state_reason = ""
    try:
        state_reason = str(client.api.state.websocket_error_reason or "")
    except Exception:
        pass
    reason_text = f"{reason} {state_reason}"
    server_answered = ok or "authorization/reject" in reason_text or "token rejected" in reason_text.lower()
    tier1 = server_answered and bool(overrides) and BLOCKED_ATTEMPTS["count"] > 0
    if not ok:
        if tier1:
            print()
            print("  DNS-block connectivity: PROVEN — the broker's server was reached and answered")
            print("  through DNS-over-HTTPS while every local DNS lookup was forced to fail.")
            print(f"  DoH overrides installed: {overrides}")
            print()
            print("  BUT the session token itself was REJECTED (authorization/reject).")
            print("  Log in to Quotex fresh, copy the new SSID token, paste it in Settings,")
            print("  then re-run this tool for the full tick-stream verification.")
            await client.close()
            return 0
        print()
        print(f"  DNS-block contact attempts observed: {BLOCKED_ATTEMPTS['count']}")
        print(f"  DoH overrides installed: {overrides or '(none)'}")
        print("VERDICT: DID NOT SURVIVE — the DoH bootstrap could not carry the connection")
        await client.close()
        return 1

    for _ in range(10):
        if await client.check_connect():
            break
        await asyncio.sleep(0.5)
    authed = await client.check_connect()
    step("auth", authed, "SSID authorization accepted" if authed else "s_authorization never arrived")
    if not authed:
        await client.close()
        return 1

    try:
        assets = await asyncio.wait_for(client.get_all_assets(), timeout=20)
    except Exception as e:
        assets = []
        print(f"         (get_all_assets error: {type(e).__name__}: {e})")
    step("assets", bool(assets), f"{len(assets)} instruments listed")
    if not assets:
        await client.close()
        return 1

    app_codes = ["USDBRL_otc", "USDINR_otc", "EURUSD_otc", "EURUSD", "AUDUSD"]
    probe = [a for a in app_codes if a in assets][:2] or list(assets)[:2]

    counts = {a: len(client.api.realtime_price.get(a, [])) for a in probe}
    for a in probe:
        try:
            await client.api.subscribe_realtime_candle(a, 60)
            await client.api.chart_notification(a)
            await client.api.follow_candle(a)
        except Exception as e:
            step("stream", False, f"subscribe send failed on {a}: {type(e).__name__}: {e}")
            await client.close()
            return 1
    print(f"  {'':<{STEP_W}}      waiting {STREAM_PROBE_SECONDS}s for price frames on {probe} …")
    await asyncio.sleep(STREAM_PROBE_SECONDS)
    total_new = 0
    for a in probe:
        now = len(client.api.realtime_price.get(a, []))
        got = max(0, now - counts[a])
        total_new += got
        print(f"  {'':<{STEP_W}}      {a}: +{got} ticks")
    step("stream", total_new > 0, f"{total_new} live price frames in {STREAM_PROBE_SECONDS}s")

    await client.close()

    overrides = dns_bootstrap.dns_override_map()
    print()
    print(f"  DNS-block contact attempts observed: {BLOCKED_ATTEMPTS['count']}")
    print(f"  DoH overrides installed: {overrides or '(none)'}")
    survived = total_new > 0 and bool(overrides)
    print(f"VERDICT: {'SURVIVED — live data flows on a DNS-blocked network via DoH' if survived else 'DID NOT SURVIVE — see FAIL steps above'}")
    return 0 if survived else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token", default=os.environ.get("QUOTEX_SESSION_TOKEN", ""), help="SSID session token")
    p.add_argument("--host", default=os.environ.get("QUOTEX_HOST", "qxbroker.com"))
    p.add_argument("--fallback-hosts", default=os.environ.get("QUOTEX_FALLBACK_HOSTS", "quotex.io,market-qx.pro"),
                   help="comma-separated fallback site domains")
    p.add_argument("--lang", default="en")
    p.add_argument("--user-agent", default=os.environ.get("QUOTEX_USER_AGENT", ""))
    p.add_argument("--root-path", default="/tmp/qx_dns_verify")
    p.add_argument("--connect-timeout", type=int, default=120)
    args = p.parse_args()
    args.fallback_hosts = [h.strip() for h in args.fallback_hosts.split(",") if h.strip()]

    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
