#!/usr/bin/env python3
"""Standalone live-connection verifier for the Quotex token pipeline.

Runs EXACTLY the steps /api/diagnose runs inside the app, but from any
shell — no deployment needed. Exit code 0 = every step passed and real
price ticks arrived; 1 = a step failed (the matrix says which one).

    python tools/verify_live_connection.py --token <SSID> [options]

Steps (each short-circuits the next):
    1. token     — present?
    2. connect   — websocket handshake + SSID authorization accepted?
    3. assets    — instrument list received?
    4. stream    — price frames pushed after subscribing?
    5. history   — candle-history endpoint answering?
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyquotex.stable_api import Quotex  # noqa: E402

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STREAM_PROBE_SECONDS = 12
STREAM_PROBE_ASSETS = 2
HISTORY_TIMEOUT_SECONDS = 10

STEP_W = 10


def step(name: str, ok: bool, detail: str) -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  {name:<{STEP_W}} [{mark}] {detail}")


async def run(args: argparse.Namespace) -> int:
    print("Quotex live-connection verification")
    print(f"  host:       {args.host}")
    print(f"  fallbacks:  {', '.join(args.fallback_hosts) or '(none)'}")
    print(f"  probe time: {STREAM_PROBE_SECONDS}s per step")

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
    step("connect", ok, f"{reason} ({time.monotonic() - t0:.1f}s, active zone: {client.api.websocket_client.current_domain() if client.api else '?'})")
    if not ok:
        await client.close()
        return 1

    # brief wait for the server's own auth reply, like the app does
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

    # pick probe assets the broker actually lists, preferring app pairs
    app_codes = [
        "USDBRL_otc", "USDINR_otc", "USDIDR_otc", "USDCOP_otc", "USDBDT_otc",
        "USDMXN_otc", "NZDUSD_otc", "USDDZD_otc", "USDPHP_otc", "USDPKR_otc",
        "USDZAR_otc", "AUDUSD_otc", "EURUSD_otc", "EURUSD", "AUDUSD",
    ]
    probe = [a for a in app_codes if a in assets][:STREAM_PROBE_ASSETS]
    probe = probe or list(assets)[:STREAM_PROBE_ASSETS]

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

    history_ok, history_count = False, 0
    if probe:
        try:
            rows = await asyncio.wait_for(
                client.get_candles(probe[0], time.time(), 60 * 40, 60),
                timeout=HISTORY_TIMEOUT_SECONDS,
            )
            history_count = len(rows or [])
            history_ok = history_count > 0
        except Exception as e:
            print(f"  {'':<{STEP_W}}      history error: {type(e).__name__}: {e}")
    step("history", history_ok, f"{history_count} candles returned for {probe[0] if probe else '-'}")

    await client.close()

    verdict = authed and bool(assets) and total_new > 0 and history_ok
    print()
    print(f"VERDICT: {'ALL STEPS PASSED — live data is flowing' if verdict else 'PIPELINE BROKEN — see the first FAIL step above'}")
    return 0 if verdict else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token", default=os.environ.get("QUOTEX_SESSION_TOKEN", ""), help="SSID session token")
    p.add_argument("--host", default=os.environ.get("QUOTEX_HOST", "qxbroker.com"))
    p.add_argument("--fallback-hosts", default=os.environ.get("QUOTEX_FALLBACK_HOSTS", "quotex.io,market-qx.pro"),
                   help="comma-separated fallback site domains")
    p.add_argument("--lang", default="en")
    p.add_argument("--user-agent", default=os.environ.get("QUOTEX_USER_AGENT", ""))
    p.add_argument("--root-path", default="/tmp/qx_verify")
    p.add_argument("--connect-timeout", type=int, default=90)
    args = p.parse_args()
    args.fallback_hosts = [h.strip() for h in args.fallback_hosts.split(",") if h.strip()]

    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
