import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app import config, db, decision, indicators
from app.quotex_client import get_client, resolve_asset_codes

logger = logging.getLogger(__name__)

TICK_POLL_SECONDS = 0.15  # ~6-7 updates/sec on the live/forming candle
BOOTSTRAP_CANDLES = 200
IN_MEMORY_CANDLES = 300

OnCandle = Callable[[str, dict[str, Any], bool], Awaitable[None]]
OnSignal = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class PairState:
    display_name: str
    asset_code: str
    candles: list[dict[str, Any]] = field(default_factory=list)
    current: dict[str, Any] | None = None
    last_tick_time: float = 0.0
    task: asyncio.Task | None = None


class FeedManager:
    def __init__(self, on_candle: OnCandle, on_signal: OnSignal) -> None:
        self.on_candle = on_candle
        self.on_signal = on_signal
        self.pairs: dict[str, PairState] = {}
        # When any pair last produced a real tick. A dead session token
        # doesn't raise anything — the websocket just goes quiet — so
        # this silence is the only signal the watchdog has to work with.
        self.last_tick_at = time.monotonic()

    def seconds_since_last_tick(self) -> float:
        return time.monotonic() - self.last_tick_at

    async def start(self) -> None:
        resolved = await resolve_asset_codes()
        client = await get_client()

        for display_name, asset_code in resolved.items():
            try:
                await self._start_pair(display_name, asset_code, client)
            except Exception:
                logger.exception("Failed to start stream for %s (%s), skipping it for now", display_name, asset_code)

        if not self.pairs:
            raise ConnectionError("Connected to Quotex but failed to start streaming for every pair")

    async def _start_pair(self, display_name: str, asset_code: str, client) -> None:
        state = PairState(display_name=display_name, asset_code=asset_code)

        history = await client.get_candles(
            asset_code, time.time(), BOOTSTRAP_CANDLES * 60, config.CANDLE_PERIOD_SECONDS
        )
        for c in history or []:
            candle = {
                "ts": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
            state.candles.append(candle)
            await db.save_candle(
                display_name, candle["ts"], candle["open"], candle["high"], candle["low"], candle["close"]
            )
        state.candles = state.candles[-IN_MEMORY_CANDLES:]
        if not history:
            logger.warning("No bootstrap history for %s (%s) — starting with an empty buffer", display_name, asset_code)

        await client.start_candles_stream(asset_code, config.CANDLE_PERIOD_SECONDS)
        state.task = asyncio.create_task(self._run_pair(state))
        self.pairs[display_name] = state
        logger.info("Started stream for %s (%s), %d bootstrap candles", display_name, asset_code, len(state.candles))

    async def _finalize_candle(self, state: PairState, candle: dict[str, Any]) -> None:
        state.candles.append(candle)
        state.candles = state.candles[-IN_MEMORY_CANDLES:]
        synthetic = bool(candle.get("synthetic"))
        await db.save_candle(
            state.display_name,
            candle["ts"],
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            synthetic=synthetic,
        )
        await self.on_candle(state.display_name, candle, True)

        if synthetic:
            # This candle is a placeholder for a minute that produced no
            # ticks — every field is the last known price repeated. Firing
            # a signal off it means predicting from data that doesn't
            # exist, and it would be graded against another placeholder.
            logger.debug("Skipping signal for %s: no ticks in this minute", state.display_name)
            return

        ind = indicators.compute(state.candles)
        dec = await decision.evaluate(state.display_name, state.candles, ind)
        if dec is not None:
            entry_ts = candle["ts"] + config.CANDLE_PERIOD_SECONDS
            target_close_ts = entry_ts + config.CANDLE_PERIOD_SECONDS
            signal_id = await db.insert_signal(
                pair=state.display_name,
                direction=dec.direction,
                entry_ts=entry_ts,
                target_close_ts=target_close_ts,
                confidence=dec.confidence,
                source=",".join(dec.sources),
                entry_price=candle["close"],
                tier=dec.tier,
            )
            await self.on_signal(
                state.display_name,
                {
                    "id": signal_id,
                    "pair": state.display_name,
                    "direction": dec.direction,
                    "confidence": dec.confidence,
                    "confirmations": dec.confirmations,
                    "sources": dec.sources,
                    "tier": dec.tier,
                    "entry_ts": entry_ts,
                    "target_close_ts": target_close_ts,
                    "result": "PENDING",
                },
            )

    def _apply_tick(self, state: PairState, tick_time: float, price: float) -> dict[str, Any] | None:
        """Aggregates one tick into the running candle. Returns the
        just-finalized candle dict if this tick crossed a minute
        boundary, else None."""
        bucket_start = int(tick_time // config.CANDLE_PERIOD_SECONDS) * config.CANDLE_PERIOD_SECONDS
        finalized = None

        if state.current is None:
            state.current = {"ts": bucket_start, "open": price, "high": price, "low": price, "close": price}
        elif bucket_start != state.current["ts"]:
            finalized = state.current
            state.current = {"ts": bucket_start, "open": price, "high": price, "low": price, "close": price}
        else:
            c = state.current
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            # A real tick landed in a minute the timer had already filled
            # in: it's genuine market data from here on.
            c.pop("synthetic", None)

        return finalized

    async def _run_pair(self, state: PairState) -> None:
        client = await get_client()
        while True:
            await asyncio.sleep(TICK_POLL_SECONDS)
            try:
                ticks = client.api.realtime_price.get(state.asset_code, [])
                new_ticks = [t for t in ticks if t["time"] > state.last_tick_time]

                if new_ticks:
                    self.last_tick_at = time.monotonic()

                for t in new_ticks:
                    finalized = self._apply_tick(state, t["time"], t["price"])
                    state.last_tick_time = t["time"]
                    if finalized is not None:
                        await self._finalize_candle(state, finalized)

                # Timer-based fallback close: keeps sparse OTC feeds moving
                # even if no tick arrives right at the minute boundary.
                if state.current is not None:
                    now = time.time()
                    boundary_end = state.current["ts"] + config.CANDLE_PERIOD_SECONDS
                    if now >= boundary_end + 1:
                        finalized = state.current
                        last_price = finalized["close"]
                        # The replacement is a placeholder for a minute
                        # that hasn't produced a single tick yet. Flag it
                        # as such — if ticks do arrive the flag is cleared
                        # below, and if they never do, nothing downstream
                        # will mistake it for market data.
                        state.current = {
                            "ts": boundary_end,
                            "open": last_price,
                            "high": last_price,
                            "low": last_price,
                            "close": last_price,
                            "synthetic": True,
                        }
                        await self._finalize_candle(state, finalized)
                    else:
                        await self.on_candle(state.display_name, state.current, False)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad iteration (e.g. a broadcast racing a client
                # (re)connect) must not permanently kill this pair's stream.
                logger.exception("Feed iteration error for %s, continuing", state.display_name)

    async def stop(self) -> None:
        """Cancels every pair's streaming task. Each _run_pair loop
        captured its own `client` reference when it started, so simply
        swapping the module-level Quotex client (e.g. after a session
        update) would leave these tasks silently talking to the old,
        stale connection — they have to be torn down and restarted
        instead of just letting the client singleton change underneath
        them."""
        tasks = [state.task for state in self.pairs.values() if state.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.pairs.clear()
