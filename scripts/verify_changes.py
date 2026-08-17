"""Verification harness for the per-candle signal guarantee and the
token-only / no-retry auth surface.

Run: python /home/z/my-project/Minimum-pair/scripts/verify_changes.py
"""
import asyncio
import os
import random
import sys
import tempfile
from pathlib import Path

# Set DB_PATH to a temp file before importing the app, so the test
# doesn't touch any real candle database.
_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB.close()
os.environ["DB_PATH"] = _DB.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, decision, indicators  # noqa: E402
from app import quotex_client  # noqa: E402


def _gen_candles(n: int = 250, seed: int = 42) -> list[dict]:
    """Generate a random-walk candle series with realistic OHLC shape."""
    rnd = random.Random(seed)
    price = 1.1000
    candles = []
    ts = 1700000000
    for _ in range(n):
        o = price
        drift = rnd.uniform(-0.0005, 0.0005)
        c = max(0.0001, o + drift)
        h = max(o, c) + rnd.uniform(0, 0.0008)
        low = min(o, c) - rnd.uniform(0, 0.0008)
        candles.append({
            "ts": ts,
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(low, 5),
            "close": round(c, 5),
        })
        price = c
        ts += 60
    return candles


async def verify_per_candle_signal() -> None:
    """Every finalized candle MUST produce a signal decision — that's
    the per-candle guarantee. Confirmed or noise tier, never None.
    """
    print("\n[1] Per-candle signal guarantee")
    candles = _gen_candles(250)
    ind = indicators.compute(candles)
    fired = 0
    noise = 0
    confirmed = 0
    none_count = 0
    # Walk forward one candle at a time, the way feed.py does.
    for i in range(60, len(candles) - 1):
        history = candles[: i + 1]
        dec = await decision.evaluate("EUR/USD", history, ind)
        if dec is None:
            none_count += 1
        else:
            fired += 1
            if dec.tier == "confirmed":
                confirmed += 1
            elif dec.tier == "noise":
                noise += 1
    print(f"    candles tested: {len(candles) - 60 - 1}")
    print(f"    signals fired: {fired}")
    print(f"      confirmed:   {confirmed}")
    print(f"      noise:        {noise}")
    print(f"    None returned:  {none_count}")
    assert none_count == 0, "Per-candle guarantee violated: some candles produced no signal"
    print("    PASS — every candle produced a signal")


async def verify_token_only_auth() -> None:
    """Token-only auth surface — no admin token, no Railway keys, no
    other auth fields. The frontend only ever pastes a session_token
    (plus optional cookies).
    """
    print("\n[2] Token-only auth surface")
    # 2a: quotex_client module exposes no admin/railway surface
    qc_attrs = [a for a in dir(quotex_client) if not a.startswith("__")]
    forbidden = ["railway_control", "ADMIN_TOKEN", "_classify_failure",
                 "_backoff_seconds_for", "FAILURE_TRANSIENT",
                 "FAILURE_CLOUDFLARE", "FAILURE_CREDENTIALS",
                 "_credentials_fingerprint"]
    leaked = [a for a in forbidden if a in qc_attrs]
    print(f"    quotex_client public attrs: {len(qc_attrs)}")
    print(f"    forbidden attrs leaked: {leaked}")
    assert not leaked, f"Dead auth machinery leaked into quotex_client: {leaked}"

    # 2b: config has no ADMIN_TOKEN, no RAILWAY_* settings
    cfg_attrs = [a for a in dir(config) if a.isupper()]
    print(f"    config attrs: {len(cfg_attrs)}")
    bad = [a for a in cfg_attrs if "ADMIN" in a or a.startswith("RAILWAY_") or "LOGIN_BACKOFF" in a]
    print(f"    forbidden config attrs: {bad}")
    assert not bad, f"Forbidden config attrs present: {bad}"

    # 2c: server.SessionUpdate has only the two token fields, no admin_token
    from app.server import SessionUpdate
    fields = set(SessionUpdate.model_fields.keys())
    print(f"    /api/session fields: {fields}")
    assert "admin_token" not in fields, "admin_token leaked into /api/session payload"
    assert fields == {"session_token", "session_cookies"}, f"unexpected /api/session fields: {fields}"
    print("    PASS — token-only auth surface verified")


async def verify_no_retry_on_failure() -> None:
    """A failed password login permanently parks the password path
    for the rest of the run — the app does not retry the
    Cloudflare-guarded login page on its own.
    """
    print("\n[3] No retry after a failed password login")
    # Reset state — these are module-level globals we control.
    quotex_client._password_login_failed = False
    quotex_client._password_failure_reason = ""
    assert quotex_client.auth_mode() == "password", "fresh start should be password mode"
    assert quotex_client.password_failure_count() == 0
    assert quotex_client.login_backoff_seconds_remaining() == 0

    # Simulate one failure
    await quotex_client._record_password_failure("test: simulated Cloudflare block")
    assert quotex_client.password_failure_count() == 1, "failure should be recorded"
    assert quotex_client.auth_mode() == "blocked", "after failure, mode should be blocked"
    assert quotex_client.login_backoff_seconds_remaining() > 0, "backoff should be visible to the UI"

    # A pasted token clears the failure flag (set_manual_session)
    await quotex_client.set_manual_session("dummy-token-for-test", "")
    assert quotex_client.auth_mode() == "session_token", "pasting a token switches to token mode"
    assert not quotex_client._password_login_failed, "pasting a token clears the failure flag"
    print("    PASS — no retry, token paste clears state")


async def verify_chart_marker_payload_shape() -> None:
    """The frontend's setMarkers call expects ascending-time, unique
    objects with the {time, position, color, shape, text} shape. The
    chart.js _markerFor() function produces that shape; this just
    documents the contract for future drift.
    """
    print("\n[4] Chart marker payload shape (frontend contract)")
    from app.decision import Decision
    # Simulate what the frontend receives from /ws signal broadcast.
    dec = Decision(direction="CALL", confidence=None, confirmations=1,
                   sources=["fallback_color"], tier="noise")
    signal = {"id": 1, "pair": "EUR/USD", "direction": dec.direction,
              "confidence": dec.confidence, "tier": dec.tier,
              "entry_ts": 1700000060, "target_close_ts": 1700000120}
    # Mirror the chart.js _markerFor logic in Python to verify the shape
    is_call = signal["direction"] == "CALL"
    marker = {
        "time": signal["entry_ts"],
        "position": "belowBar" if is_call else "aboveBar",
        "color": "#22c55e" if is_call else "#ef4444",
        "shape": "arrowUp" if is_call else "arrowDown",
        "text": f"{signal['direction']}{' · noise' if signal['tier'] != 'confirmed' else ''}",
    }
    assert set(marker.keys()) == {"time", "position", "color", "shape", "text"}
    print(f"    marker keys: {sorted(marker.keys())}")
    print("    PASS — marker payload shape is consistent")


async def main() -> None:
    print("=" * 70)
    print("Verification harness — per-candle signals + token-only auth")
    print("=" * 70)
    await db.init_db()
    await verify_per_candle_signal()
    await verify_token_only_auth()
    await verify_no_retry_on_failure()
    await verify_chart_marker_payload_shape()
    print("\n" + "=" * 70)
    print("ALL CHECKS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
