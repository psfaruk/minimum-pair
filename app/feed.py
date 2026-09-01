import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app import config, db, decision, indicators
from app.quotex_client import get_client, resolve_asset_codes

logger = logging.getLogger(__name__)

# 2026-08: tighter tick polling (~20 Hz) so the 0-second signal fires
# within ~50ms of the candle boundary instead of the old ~150ms. Costs
# slightly more CPU per pair but is essential for the 0-second entry
# requirement on Quotex 1-minute binary options.
TICK_POLL_SECONDS = 0.05
BOOTSTRAP_CANDLES = 200
IN_MEMORY_CANDLES = 300

# The broker's raw-tick history endpoint caps each reply at the most
# recent ~1000-1600 ticks (~10-12 minutes) regardless of the requested
# window, so a single get_candles(asset, now, 12000, 60) call returned
# only 6-13 candles instead of the full 200-candle warmup the engine
# needs — every pair started nearly blind and the indicator/pattern
# stack starved. The AGGREGATED history endpoint does honor deep ranges
# (~40 candles per request, verified at 10h+ depth), so bootstrap pulls
# the full window as consecutive chunks and merges them.
BOOTSTRAP_CHUNKS = 5
BOOTSTRAP_CANDLES_PER_CHUNK = 40  # 5 x 40 = 200 candles ≈ 3.3 hours
BOOTSTRAP_CHUNK_TIMEOUT_SECONDS = 10

# How stale a tick may be and still be folded into its (already
# finalized) candle. Quotex OTC ticks can arrive late — in bursts after
# a brief websocket stall — and a tick timestamped inside a minute that
# already closed used to re-open that minute: it finalized the running
# placeholder early, re-created the old minute as a one-tick candle, and
# fired a second signal for the same entry minute (222 same-minute
# CALL+PUT contradictions and 422 duplicate entry minutes in one day of
# production data). Ticks older than this window are dropped as junk —
# by then the minute's outcome is the candle/backfill's business.
LATE_TICK_MAX_AGE_SECONDS = 2 * config.CANDLE_PERIOD_SECONDS

# Round-trip budget for the background real-candle backfill (a WS
# request/response to Quotex's own history endpoint). Short enough to
# comfortably land within evaluator.py's SYNTHETIC_GRACE_SECONDS window,
# generous enough to survive normal network jitter.
BACKFILL_TIMEOUT_SECONDS = 8

# pyquotex correlates a get_candles() response to its request through one
# shared string on the connection (the most recent "status" control frame),
# not a per-request id. With 16 pairs' candles finalizing in the same
# second, firing get_candles() concurrently means their requests and
# replies interleave on that shared state and every one of them times out
# — observed 100% failure firing them concurrently. Serializing through
# this lock keeps at most one history/load round trip in flight at a
# time, which is what actually lets any of them succeed.
_BACKFILL_LOCK = asyncio.Lock()

# Safety net against unbounded backlog growth: if backfills are being
# produced faster than the serialized queue can drain them (e.g. every
# attempt is timing out for some other reason), stop piling new tasks
# behind the lock instead of growing without limit.
_backfill_queue_depth = 0
MAX_BACKFILL_QUEUE_DEPTH = 24

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
    # The most recent regime read for this pair ("trend" | "range" |
    # "neutral") — surfaced via /api/status so consumers can see what
    # market condition the engine believed it was trading into.
    last_regime: str = "neutral"


class FeedManager:
    def __init__(self, on_candle: OnCandle, on_signal: OnSignal) -> None:
        self.on_candle = on_candle
        self.on_signal = on_signal
        self.pairs: dict[str, PairState] = {}
        # When any pair last produced a real tick. A dead session token
        # doesn't raise anything — the websocket just goes quiet — so
        # this silence is the only signal the watchdog has to work with.
        self.last_tick_at = time.monotonic()
        # Lifetime counter of real ticks folded across every pair. A
        # deployed instance that sits "connected" with total_ticks
        # stuck at 0 is in the auth-works-but-no-stream state (region
        # block, closed market for every pair, subscription dropped) —
        # /api/status exposes it so that state is visible from outside.
        self.total_ticks = 0

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

    async def _bootstrap_history(self, asset_code: str, client) -> list[dict[str, Any]]:
        """Fetches the full 200-candle warmup window as consecutive
        merged chunks (see BOOTSTRAP_CHUNKS for why single-request
        history cannot deliver this). Falls back to one wide request if
        the chunked path yields nothing."""
        period = config.CANDLE_PERIOD_SECONDS
        chunk_seconds = BOOTSTRAP_CANDLES_PER_CHUNK * period
        merged: dict[int, dict[str, Any]] = {}
        end_ts = int(time.time())

        for i in range(BOOTSTRAP_CHUNKS):
            chunk_end = end_ts - i * chunk_seconds
            try:
                rows = await client.get_candles(
                    asset_code,
                    chunk_end,
                    chunk_seconds,
                    period,
                    timeout=BOOTSTRAP_CHUNK_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning(
                    "Bootstrap history chunk %d failed for %s",
                    i, asset_code, exc_info=True,
                )
                rows = None
            for c in rows or []:
                try:
                    ts = int(c["time"])
                    merged[ts] = {
                        "ts": ts,
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue

        if not merged:
            # Last resort: the legacy single wide request (returns only
            # the recent tick-capped window, but better than nothing).
            try:
                rows = await client.get_candles(
                    asset_code, end_ts, BOOTSTRAP_CANDLES * period, period
                )
            except Exception:
                rows = None
            for c in rows or []:
                try:
                    ts = int(c["time"])
                    merged[ts] = {
                        "ts": ts,
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue

        return [merged[ts] for ts in sorted(merged)]

    async def _start_pair(self, display_name: str, asset_code: str, client) -> None:
        state = PairState(display_name=display_name, asset_code=asset_code)

        history = await self._bootstrap_history(asset_code, client)
        for c in history:
            state.candles.append(c)
            await db.save_candle(
                display_name, c["ts"], c["open"], c["high"], c["low"], c["close"]
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
            # This candle is a placeholder for a minute our own tick
            # polling produced no ticks for — every field is the last
            # known price repeated. Firing a signal off it means
            # predicting from data that doesn't exist, and it would be
            # graded against another placeholder. But the broker's own
            # feed almost always kept moving even when our polling missed
            # it, so fetch the real OHLC in the background (never
            # blocking the 0-second signal path for the NEXT candle) and
            # correct the stored row once it's back. evaluator.py gives
            # this a short grace window before conceding a draw.
            global _backfill_queue_depth
            if _backfill_queue_depth < MAX_BACKFILL_QUEUE_DEPTH:
                _backfill_queue_depth += 1
                asyncio.create_task(self._backfill_real_candle(state, candle))
            else:
                logger.warning(
                    "Backfill queue at capacity (%d) — leaving %s @ %s as a placeholder",
                    _backfill_queue_depth, state.display_name, candle["ts"],
                )
            logger.debug("Skipping signal for %s: no ticks in this minute", state.display_name)
            return

        # Synthetic fillers are fabricated flat candles (open=high=low=close,
        # the last known price repeated) inserted only to keep the minute
        # sequence unbroken. Feeding them into indicators/patterns/reaction
        # detection would let a fake zero-range bar shape an SMA/EMA/RSI
        # window or masquerade as a doji/harami against a real neighbour —
        # the same reason pattern_miner already excludes them from its
        # training windows. Compact them out so every downstream detector
        # only ever sees real market data.
        entry_ts = candle["ts"] + config.CANDLE_PERIOD_SECONDS

        # One signal per entry minute, no exceptions. A second finalize
        # of the same candle can only be a feed race (the late-tick
        # double-finalize this file now guards against, or a restart
        # replaying a boundary) — and a duplicate signal is worse than
        # none: it contradicts the first in the UI and grades the same
        # sources twice for the same trade.
        if await db.signal_exists(state.display_name, entry_ts):
            logger.debug(
                "A signal already exists for %s @ %s — skipping duplicate",
                state.display_name, entry_ts,
            )
            return

        # A candle finalized this far past its boundary (event-loop
        # stall, deploy freeze, DB congestion) would produce a signal
        # whose entry minute has already closed. It would be graded
        # instantly against a candle nobody predicted and shown as
        # though it were current — a lie either way. Honest to skip it.
        if time.time() - entry_ts >= config.CANDLE_PERIOD_SECONDS:
            logger.warning(
                "Candle %s @ %s finalized %ds late — entry minute already closed, skipping signal",
                state.display_name, candle["ts"], int(time.time() - entry_ts),
            )
            return

        clean_candles = [c for c in state.candles if not c.get("synthetic")]
        ind = indicators.compute(clean_candles)
        dec = await decision.evaluate(state.display_name, clean_candles, ind)
        if dec is not None:
            state.last_regime = dec.regime
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
                all_sources=[{"source": s, "direction": d} for s, d in dec.all_votes],
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
                    "regime": dec.regime,
                    "entry_ts": entry_ts,
                    "target_close_ts": target_close_ts,
                    "result": "PENDING",
                },
            )

    async def _backfill_real_candle(self, state: PairState, candle: dict[str, Any]) -> None:
        """Replaces a fabricated flat candle with the broker's own OHLC
        for that minute, once it's available.

        Our tick polling missing a minute doesn't mean the market was
        flat — it means our websocket subscription didn't happen to
        observe a tick, which turned out to be true for ~45% of minutes
        on some pairs. Quotex's own history endpoint aggregates from its
        real feed server-side, independent of what our client happened to
        see, so it almost always has the real bar. This runs as a
        fire-and-forget background task so it never delays the
        0-second signal for the next candle."""
        global _backfill_queue_depth
        try:
            try:
                client = await get_client()
                async with _BACKFILL_LOCK:
                    rows = await client.get_candles(
                        state.asset_code,
                        candle["ts"] + config.CANDLE_PERIOD_SECONDS,
                        config.CANDLE_PERIOD_SECONDS,
                        config.CANDLE_PERIOD_SECONDS,
                        timeout=BACKFILL_TIMEOUT_SECONDS,
                        use_cache=True,
                    )
            except Exception:
                logger.debug(
                    "Backfill fetch failed for %s @ %s", state.display_name, candle["ts"], exc_info=True
                )
                return

            real = next((r for r in (rows or []) if int(r.get("time", -1)) == candle["ts"]), None)
            if real is None:
                return  # broker has nothing for this minute either — the placeholder stands

            o, h, l, c = float(real["open"]), float(real["high"]), float(real["low"]), float(real["close"])
            if h == l:
                # The broker itself reports a flat/no-trade minute — the
                # fabricated placeholder already happened to be the right
                # answer, nothing to correct.
                return

            await db.save_candle(state.display_name, candle["ts"], o, h, l, c, synthetic=False)
            for c_ in state.candles:
                if c_["ts"] == candle["ts"]:
                    c_.update(open=o, high=h, low=l, close=c)
                    c_.pop("synthetic", None)
                    break
            logger.debug("Backfilled real candle for %s @ %s", state.display_name, candle["ts"])
        finally:
            _backfill_queue_depth -= 1

    def _apply_tick(self, state: PairState, bucket: int, price: float) -> dict[str, Any] | None:
        """Aggregates one tick into the running candle. Returns the
        just-finalized candle dict if this tick crossed a minute
        boundary, else None.

        Callers must route ticks from already-finalized minutes to
        _handle_tick() instead — re-opening a closed minute here is what
        produced duplicate signals and fabric candles."""
        finalized = None

        if state.current is None:
            state.current = {"ts": bucket, "open": price, "high": price, "low": price, "close": price}
        elif bucket != state.current["ts"]:
            finalized = state.current
            state.current = {"ts": bucket, "open": price, "high": price, "low": price, "close": price}
        else:
            c = state.current
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            # A real tick landed in a minute the timer had already filled
            # in: it's genuine market data from here on.
            c.pop("synthetic", None)

        return finalized

    def _handle_tick(
        self,
        state: PairState,
        bucket: int,
        price: float,
        late_updates: dict[int, tuple[dict[str, Any], float]],
    ) -> dict[str, Any] | None:
        """Routes one tick into the running candle, guarding the boundary
        that the duplicate-signal race lived on.

        A tick whose minute already closed is folded into that finalized
        candle instead of re-opening it: the candle's high/low/close are
        corrected in memory (and flushed to the DB + any PENDING signal
        by _flush_late_updates), and a placeholder minute that finally
        saw a real tick loses its synthetic flag. `late_updates` maps
        bucket -> (candle, close_before_this_burst) so the flush can
        write each candle once per burst with its final value."""
        if state.current is not None and bucket < state.current["ts"]:
            if state.current["ts"] - bucket > LATE_TICK_MAX_AGE_SECONDS:
                return None  # ancient tick (clock jitter/replay) — not evidence
            candle = next((c for c in reversed(state.candles) if c["ts"] == bucket), None)
            if candle is None:
                return None  # older than the in-memory window — nothing to correct
            late_updates.setdefault(bucket, (candle, candle["close"]))
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle.pop("synthetic", None)
            return None
        return self._apply_tick(state, bucket, price)

    async def _flush_late_updates(
        self, state: PairState, late_updates: dict[int, tuple[dict[str, Any], float]]
    ) -> None:
        """Persists the late-tick folds collected by _handle_tick.

        One write per candle per burst, ordered, so a burst's last tick
        for a minute is what lands. A folded candle was finalized by the
        timer as a placeholder, its synthetic flag is cleared here — real
        tick evidence arrived. Any PENDING signal that used the old close
        as its entry price is re-priced to the corrected one, so grading
        compares against the price the minute actually ended at."""
        for bucket, (candle, old_close) in late_updates.items():
            await db.save_candle(
                state.display_name,
                bucket,
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                synthetic=False,
            )
            if candle["close"] != old_close:
                await db.update_pending_entry_price(
                    state.display_name,
                    bucket + config.CANDLE_PERIOD_SECONDS,
                    old_close,
                    candle["close"],
                )

    async def _run_pair(self, state: PairState) -> None:
        client = await get_client()
        while True:
            await asyncio.sleep(TICK_POLL_SECONDS)
            try:
                ticks = client.api.realtime_price.get(state.asset_code, [])
                new_ticks = [t for t in ticks if t["time"] > state.last_tick_time]

                if new_ticks:
                    self.last_tick_at = time.monotonic()
                    self.total_ticks += len(new_ticks)

                late_updates: dict[int, tuple[dict[str, Any], float]] = {}
                for t in new_ticks:
                    bucket = int(t["time"] // config.CANDLE_PERIOD_SECONDS) * config.CANDLE_PERIOD_SECONDS
                    finalized = self._handle_tick(state, bucket, t["price"], late_updates)
                    state.last_tick_time = max(state.last_tick_time, t["time"])
                    if finalized is not None:
                        await self._finalize_candle(state, finalized)
                await self._flush_late_updates(state, late_updates)

                # Timer-based fallback close: keeps sparse OTC feeds moving
                # even if no tick arrives right at the minute boundary.
                # 2026-08: previously waited boundary_end + 1 before
                # finalizing, which delayed the 0-second signal for the
                # NEXT candle by ~1s on every sparse minute. Now we
                # finalize exactly at boundary_end so the signal for the
                # new candle is computed and broadcast at the 0-second
                # mark, as the user requirement specifies.
                if state.current is not None:
                    now = time.time()
                    boundary_end = state.current["ts"] + config.CANDLE_PERIOD_SECONDS
                    if now >= boundary_end:
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
