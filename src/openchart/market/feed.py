"""Live feed: gap-fill on start and after every reconnect, then stream ticks.

M1 scope: ticks are stored to Parquet and logged. M2 adds the candle builder that
turns them into live candles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta

from openchart.market.backfill import backfill, gap_fill
from openchart.providers.base import Provider, Tick
from openchart.providers.stream import ResilientTickStream
from openchart.store.candles import CandleStore
from openchart.store.ticks import TickWriter

log = logging.getLogger(__name__)


class Feed:
    def __init__(
        self,
        provider: Provider,
        store: CandleStore,
        instruments: list[str],
        *,
        tick_writer: TickWriter | None = None,
        backfill_years: float = 2.0,
        flush_interval: float = 60.0,
        status_interval: float = 60.0,
    ):
        self.provider = provider
        self.store = store
        self.instruments = instruments
        self.tick_writer = tick_writer
        self.backfill_years = backfill_years
        self.flush_interval = flush_interval
        self.status_interval = status_interval
        self.stream = ResilientTickStream(provider, instruments, on_connected=self._on_connected)
        self.last_tick: dict[str, Tick] = {}
        self.tick_count = 0

    async def fill_history(self) -> None:
        for instrument in self.instruments:
            if await self.store.last_ts(self.provider.name, instrument) is None:
                start = datetime.now(UTC) - timedelta(days=365.25 * self.backfill_years)
                log.info("no history for %s, backfilling from %s", instrument, start.date())
                result = await backfill(self.provider, self.store, instrument, start)
            else:
                result = await gap_fill(self.provider, self.store, instrument)
            log.info("%s: history up to %s (%d new)", instrument, result.last_ts, result.written)

    async def _on_connected(self, reconnect: bool) -> None:
        if reconnect:
            await self.fill_history()

    async def _periodic(self) -> None:
        last_flush = last_status = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if self.tick_writer and now - last_flush >= self.flush_interval:
                last_flush = now
                await self._flush()
            if now - last_status >= self.status_interval:
                last_status = now
                self.log_status()

    async def _flush(self) -> None:
        if self.tick_writer and self.tick_writer.pending:
            await asyncio.to_thread(self.tick_writer.flush)
            await asyncio.to_thread(self.tick_writer.compact_closed_days)

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
        try:
            async for tick in self.stream.ticks():
                self.tick_count += 1
                self.last_tick[tick.instrument] = tick
                if self.tick_writer:
                    self.tick_writer.add(tick)
        finally:
            periodic.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic
            if self.tick_writer and self.tick_writer.pending:
                self.tick_writer.flush()
