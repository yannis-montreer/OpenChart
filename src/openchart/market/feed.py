"""Live feed: history, tick stream, live candles.

On start (and after every reconnect) it gap-fills M1 from REST and repairs holes in the
last few days, then streams ticks. When the process was paused (the computer slept), the
repair also runs as soon as it wakes up. An instrument without any stored history (just
added, or its download was interrupted) downloads it in the background, newest first,
so the instruments already there keep streaming; all instruments share the one price
stream.
Each tick goes to the candle builder, the tick store and any listeners (the websocket
hub). Closed M1 bars are written in small batches, and once a minute the last few
minutes are re-fetched from the provider so stored M1 matches the broker's candles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from openchart.market.backfill import (
    CLOSURE_MINUTES,
    DEEP_HISTORY_START,
    RepairResult,
    deep_fill,
    download_backward,
    gap_fill,
    refresh_recent,
    repair,
    repair_recent,
)
from openchart.market.cache import CandleCache
from openchart.market.candle_builder import CandleBuilder
from openchart.market.sessions import TradingHours
from openchart.providers.base import Candle, Provider, Tick
from openchart.providers.stream import ResilientTickStream
from openchart.store.candles import CandleStore
from openchart.store.ticks import TickWriter

log = logging.getLogger(__name__)

TickListener = Callable[[Tick], None]
ClosuresListener = Callable[[str], Awaitable[None]]
WakeListener = Callable[[float, float], None]  # frozen from, to (wall clock)

# A once-a-second loop that took this long means the process was paused (sleep, hibernate).
WAKE_SECONDS = 30.0
# A failed history download is tried again after this long.
DOWNLOAD_RETRY_SECONDS = 60.0


def _history() -> dict[str, Any]:
    """An instrument's download status, as shown in the page."""
    return {
        "state": "done",  # pending | running | failed | done
        "phase": None,  # h1 | m1 while running
        "written": 0,
        "oldest": None,  # oldest candle reached so far
        "target": None,  # where the M1 download stops
        "progress": None,  # 0..1 of the current phase
        "error": None,
    }


class Feed:
    def __init__(
        self,
        provider: Provider,
        store: CandleStore,
        instruments: list[str],
        *,
        hours: dict[str, TradingHours | None] | None = None,
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
        self.hours = hours or {}  # per instrument, to tell holes from market closures
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
        # Called with the instrument when its learned closures changed.
        self.on_closures: ClosuresListener | None = None
        # Called when the process was frozen (the computer slept), before the repair.
        self.on_wake: WakeListener | None = None
        self.last_tick: dict[str, Tick] = {}
        self.tick_count = 0
        self.history: dict[str, dict[str, Any]] = {name: _history() for name in instruments}
        self._pending_m1: list[tuple[str, Candle]] = []
        # Instruments whose download (H1 or M1) is not finished, from the stored marks.
        self.downloading: set[str] = set()
        self.download_retry = DOWNLOAD_RETRY_SECONDS

    # History

    async def find_downloads(self) -> None:
        for instrument in self.instruments:
            h1_from, m1_from = await self.store.download_marks(self.source, instrument)
            if m1_from is None or (self.deep_history and h1_from is None):
                self.downloading.add(instrument)
                self.history[instrument]["state"] = "pending"

    async def fill_history(self) -> None:
        """Bring stored M1 up to the present and repair recent holes: a few requests.
        An instrument without any M1 yet is left to its download."""
        for instrument in self.instruments:
            if await self.store.last_ts(self.source, instrument) is None:
                continue
            result = await gap_fill(self.provider, self.store, instrument)
            await self._repaired(
                await repair_recent(
                    self.provider, self.store, instrument, self.hours.get(instrument)
                )
            )
            log.info("%s: history up to %s (%d new)", instrument, result.last_ts, result.written)

    async def download(self, instrument: str) -> None:
        """An instrument's history, newest first: all of H1 (1h and higher charts are
        complete within seconds), then M1 back to `backfill_years`. Resumes from the
        oldest stored candle; a failure is retried without stopping anything else."""
        info = self.history[instrument]
        now = time.time()
        target = int(now - 365.25 * 86400 * self.backfill_years)

        def progress_for(phase: str, floor: float):
            def progress(written: int, ts: int, direction: str) -> None:
                done = (now - ts) / max(1.0, now - floor)
                info.update(phase=phase, written=written, oldest=ts, progress=min(1.0, done))

            return progress

        while True:
            info.update(state="running", error=None)
            try:
                if self.deep_history:
                    info.update(phase="h1", written=0, target=None, progress=0.0, oldest=None)
                    await deep_fill(
                        self.provider,
                        self.store,
                        instrument,
                        progress=progress_for("h1", DEEP_HISTORY_START.timestamp()),
                    )
                _, m1_from = await self.store.download_marks(self.source, instrument)
                if m1_from is None:
                    log.info("downloading %s M1 back to %s, newest first", instrument, target)
                    first = await self.store.first_ts(self.source, instrument)
                    info.update(
                        phase="m1",
                        written=0,
                        target=target,
                        oldest=first,
                        progress=0.0 if first is None else (now - first) / (now - target),
                    )
                    await download_backward(
                        self.provider,
                        self.store,
                        instrument,
                        target,
                        progress=progress_for("m1", target),
                    )
                break
            except Exception as exc:
                info.update(state="failed", error=f"{type(exc).__name__}: {exc}")
                log.warning(
                    "downloading %s history failed (%s), retrying in %.0fs",
                    instrument,
                    exc,
                    self.download_retry,
                )
                await asyncio.sleep(self.download_retry)
        self.downloading.discard(instrument)
        info.update(_history(), oldest=await self.store.first_ts(self.source, instrument))
        log.info("%s: history downloaded", instrument)

    async def fill_deep_history_and_cache(self) -> None:
        """Downloads, closures scan and bar cache, in the background so live ticks flow
        straight away. Instruments with complete history go first: theirs is quick."""
        for instrument in sorted(self.instruments, key=lambda i: i in self.downloading):
            if instrument in self.downloading:
                await self.download(instrument)
            elif self.deep_history:
                try:
                    await deep_fill(self.provider, self.store, instrument)
                except Exception as exc:
                    log.warning("deep history for %s failed: %s", instrument, exc)
            await self.scan_closures(instrument)
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

    async def repair(self) -> None:
        """Refill holes in the last few days of M1."""
        for instrument in self.instruments:
            try:
                await self._repaired(
                    await repair_recent(
                        self.provider, self.store, instrument, self.hours.get(instrument)
                    )
                )
            except Exception as exc:
                log.warning("repairing recent M1 for %s failed: %s", instrument, exc)

    async def repair_instrument(self, instrument: str) -> None:
        """The tail and recent holes of one instrument (before the alert catch-up)."""
        await gap_fill(self.provider, self.store, instrument)
        await self._repaired(
            await repair_recent(self.provider, self.store, instrument, self.hours.get(instrument))
        )

    async def scan_closures(self, instrument: str) -> None:
        """Once per instrument: learn closures over all stored M1 (about one request per
        holiday). Later ones are learned by the recent repair as they happen."""
        hours = self.hours.get(instrument)
        if instrument in self.downloading:
            return  # over all of M1, so once it is downloaded
        if hours is None or await self.store.closures_scanned(self.source, instrument):
            return
        try:
            result = await repair(
                self.provider, self.store, instrument, hours, min_open_minutes=CLOSURE_MINUTES
            )
        except Exception as exc:
            log.warning("scanning %s for market closures failed: %s", instrument, exc)
            return
        await self.store.mark_closures_scanned(self.source, instrument, int(time.time()))
        n = len(await self.store.closures(self.source, instrument))
        log.info("%s: %d market closures (holidays, early closes) learned", instrument, n)
        await self._repaired(result)

    async def _repaired(self, result: RepairResult) -> None:
        if result.closures_changed and self.on_closures is not None:
            try:
                await self.on_closures(result.instrument)
            except Exception:
                log.exception("closures listener failed")

    async def refresh(self) -> None:
        for instrument in self.instruments:
            try:
                await refresh_recent(self.provider, self.store, instrument)
            except Exception as exc:
                log.warning("refreshing recent M1 for %s failed: %s", instrument, exc)

    # Background work

    async def _periodic(self) -> None:
        last_flush = last_status = last_refresh = time.monotonic()
        last_wall = time.time()
        while True:
            await asyncio.sleep(1)
            wall = time.time()
            woke = wall - last_wall > WAKE_SECONDS
            if woke:
                log.info("resumed after %.0fs paused; checking recent history", wall - last_wall)
                if self.on_wake is not None:
                    try:
                        self.on_wake(last_wall, wall)
                    except Exception:
                        log.exception("wake listener failed")
            last_wall = wall
            self.builder.on_clock(wall)
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
            if woke:
                # The refresh wrote the last few minutes, ahead of the pause: fill what lies
                # between. The stream's reconnect does it too, but only if it notices.
                await self.repair()
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
        await self.find_downloads()
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
