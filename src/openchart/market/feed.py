"""Live feed: history, tick stream, live candles.

On start (and after every reconnect) it gap-fills M1 from REST, then streams ticks.
Each tick goes to the candle builder, the tick store and any listeners (the websocket
hub). Closed M1 bars are written in small batches, and once a minute the last few
minutes are re-fetched from the provider so stored M1 matches the broker's candles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from openchart.market.backfill import backfill, deep_fill, gap_fill, refresh_recent
from openchart.market.cache import CandleCache
from openchart.market.candle_builder import CandleBuilder
from openchart.providers.base import Candle, Provider, Tick
from openchart.providers.stream import ResilientTickStream
from openchart.store.candles import CandleStore
from openchart.store.ticks import TickWriter

log = logging.getLogger(__name__)

TickListener = Callable[[Tick], None]


class Feed:
    def __init__(
        self,
        provider: Provider,
        store: CandleStore,
        instruments: list[str],
        *,
        tick_writer: TickWriter | None = None,
        builder: CandleBuilder | None = None,
        cache: CandleCache | None = None,
        deep_history: bool = True,
        backfill_years: float = 2.0,
        flush_interval: float = 60.0,
        status_interval: float = 60.0,
        refresh_interval: float = 60.0,
    ):
        self.provider = provider
        self.source = provider.name
        self.store = store
        self.instruments = instruments
        self.tick_writer = tick_writer
        self.backfill_years = backfill_years
        self.flush_interval = flush_interval
        self.status_interval = status_interval
        self.refresh_interval = refresh_interval
        self.builder = builder or CandleBuilder()
        self.cache = cache
        self.deep_history = deep_history
        self._background: asyncio.Task[None] | None = None
        self.builder.on_m1_closed = self._queue_m1
        self.builder.load_m1 = self._load_m1
        self.stream = ResilientTickStream(provider, instruments, on_connected=self._on_connected)
        self.tick_listeners: list[TickListener] = []
        self.last_tick: dict[str, Tick] = {}
        self.tick_count = 0
        self.history: dict[str, dict[str, Any]] = {
            name: {"state": "pending", "written": 0, "at": None, "deep": None, "deep_from": None}
            for name in instruments
        }
        self._pending_m1: list[tuple[str, Candle]] = []

    # History

    async def fill_history(self) -> None:
        for instrument in self.instruments:
            info = self.history[instrument]

            def progress(written: int, ts: int, direction: str, info=info) -> None:
                info.update(written=written, at=ts)

            info["state"] = "running"
            try:
                if await self.store.last_ts(self.source, instrument) is None:
                    start = datetime.now(UTC) - timedelta(days=365.25 * self.backfill_years)
                    log.info("no history for %s, backfilling from %s", instrument, start.date())
                    result = await backfill(
                        self.provider, self.store, instrument, start, progress=progress
                    )
                else:
                    result = await gap_fill(self.provider, self.store, instrument)
            except Exception:
                info["state"] = "failed"
                raise
            info.update(state="done", at=result.last_ts)
            log.info("%s: history up to %s (%d new)", instrument, result.last_ts, result.written)

    async def fill_deep_history_and_cache(self) -> None:
        """Deep H1 history (first run: everything the provider has), then the bar cache.

        Runs in the background so live ticks start flowing straight away.
        """
        for instrument in self.instruments:
            info = self.history[instrument]
            if self.deep_history:
                info["deep"] = "running"
                try:
                    result = await deep_fill(self.provider, self.store, instrument)
                    info.update(deep="done", deep_from=result.first_ts)
                except Exception as exc:
                    info["deep"] = "failed"
                    log.warning("deep history for %s failed: %s", instrument, exc)
            await self.extend_cache(instrument)

    async def extend_cache(self, instrument: str) -> None:
        if self.cache is None:
            return
        try:
            written = await self.cache.extend_all(self.source, instrument)
            if written > 10:
                log.info("cached %d derived bars for %s", written, instrument)
        except Exception:
            log.exception("extending the bar cache for %s failed", instrument)

    async def _on_connected(self, reconnect: bool) -> None:
        if reconnect:
            for instrument in self.instruments:
                self.builder.mark_gap(instrument)
            await self.fill_history()

    async def _load_m1(self, instrument: str, start: int, end: int) -> list[Candle]:
        return await self.store.read_m1(self.source, instrument, start, end)

    # Ticks

    def handle_tick(self, tick: Tick) -> None:
        self.tick_count += 1
        self.last_tick[tick.instrument] = tick
        if self.tick_writer:
            self.tick_writer.add(tick)
        # Listeners first, so clients see the tick before the candle update it causes.
        for listener in self.tick_listeners:
            try:
                listener(tick)
            except Exception:
                log.exception("tick listener failed")
        self.builder.on_tick(tick)

    def _queue_m1(self, instrument: str, candle: Candle) -> None:
        self._pending_m1.append((instrument, candle))

    async def flush_m1(self) -> None:
        pending, self._pending_m1 = self._pending_m1, []
        by_instrument: dict[str, list[Candle]] = {}
        for instrument, candle in pending:
            by_instrument.setdefault(instrument, []).append(candle)
        for instrument, candles in by_instrument.items():
            await self.store.upsert_m1(self.source, instrument, candles)

    async def refresh(self) -> None:
        for instrument in self.instruments:
            try:
                await refresh_recent(self.provider, self.store, instrument)
            except Exception as exc:
                log.warning("refreshing recent M1 for %s failed: %s", instrument, exc)

    # Background work

    async def _periodic(self) -> None:
        last_flush = last_status = last_refresh = time.monotonic()
        while True:
            await asyncio.sleep(1)
            self.builder.on_clock(time.time())
            if self._pending_m1:
                await self.flush_m1()
            now = time.monotonic()
            if self.tick_writer and now - last_flush >= self.flush_interval:
                last_flush = now
                await self._flush_ticks()
            if now - last_refresh >= self.refresh_interval:
                last_refresh = now
                await self.refresh()
                for instrument in self.instruments:
                    await self.extend_cache(instrument)
            if now - last_status >= self.status_interval:
                last_status = now
                self.log_status()

    async def _flush_ticks(self) -> None:
        if self.tick_writer and self.tick_writer.pending:
            await asyncio.to_thread(self.tick_writer.flush)
            await asyncio.to_thread(self.tick_writer.compact_closed_days)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "provider": self.source,
            "provider_state": self.stream.state.value,
            "reconnects": self.stream.reconnects,
            "instruments": {
                name: {
                    "last_tick_age": round(now - t.ts, 1)
                    if (t := self.last_tick.get(name))
                    else None,
                    "history": self.history.get(name),
                }
                for name in self.instruments
            },
        }

    def log_status(self) -> None:
        ages = {
            name: f"{time.time() - tick.ts:.0f}s" for name, tick in sorted(self.last_tick.items())
        }
        log.info(
            "feed %s, %d ticks, reconnects %d, last tick age %s",
            self.stream.state.value,
            self.tick_count,
            self.stream.reconnects,
            ages or "n/a",
        )

    async def run(self) -> None:
        await self.fill_history()
        if self.tick_writer:
            await asyncio.to_thread(self.tick_writer.compact_closed_days)
        periodic = asyncio.create_task(self._periodic())
        self._background = asyncio.create_task(self.fill_deep_history_and_cache())
        try:
            async for tick in self.stream.ticks():
                self.handle_tick(tick)
        finally:
            for task in (periodic, self._background):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            with contextlib.suppress(Exception):
                await self.flush_m1()
            if self.tick_writer and self.tick_writer.pending:
                self.tick_writer.flush()
