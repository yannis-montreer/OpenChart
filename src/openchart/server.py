"""Server lifecycle: database, provider, feed, candle builder and websocket hub."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from openchart.api.hub import Hub
from openchart.config import Config
from openchart.market.cache import CandleCache
from openchart.market.candle_builder import CandleBuilder
from openchart.market.feed import Feed
from openchart.market.series import SeriesReader
from openchart.providers.base import AuthError, Provider
from openchart.providers.registry import create_provider
from openchart.providers.stream import StreamState
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from openchart.store.ticks import TickWriter

log = logging.getLogger(__name__)

DEFAULT_SOURCE = "oanda"
FEED_RETRY_SECONDS = 30.0
STATUS_INTERVAL = 5.0


class Server:
    def __init__(self, config: Config, provider: Provider | None = None, run_feed: bool = True):
        self.config = config
        self._provider = provider
        self.run_feed = run_feed
        self.feed: Feed | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.feed_error: str | None = None

    async def start(self) -> None:
        cfg = self.config
        self.db = await Database.open(cfg.db_path)
        self.store = CandleStore(self.db.conn)
        if self._provider is None and cfg.oanda.configured:
            self._provider = create_provider(cfg)
        self.provider = self._provider
        self.source = self.provider.name if self.provider else DEFAULT_SOURCE

        self.builder = CandleBuilder()
        self.cache = CandleCache(self.store)
        self.tick_writer = TickWriter(cfg.ticks_dir, self.source) if cfg.store_ticks else None
        self.hub = Hub(self.builder, status=self.status)
        self.reader = SeriesReader(
            self.store,
            cfg.ticks_dir if cfg.store_ticks else None,
            self.tick_writer.buffered if self.tick_writer else None,
            cache=self.cache,
        )

        rows = await self.store.instruments(self.source, enabled_only=True)
        if self.provider is not None and rows:
            self.feed = Feed(
                self.provider,
                self.store,
                [r.instrument for r in rows],
                tick_writer=self.tick_writer,
                builder=self.builder,
                cache=self.cache,
                deep_history=cfg.deep_history,
                backfill_years=cfg.backfill_years,
            )
            self.feed.tick_listeners.append(self.hub.on_tick)
            self.feed.stream.on_state = self._on_stream_state
            if self.run_feed:
                self._tasks.append(asyncio.create_task(self._run_feed(), name="feed"))
        elif self.provider is None:
            log.warning("no data provider configured; serving stored data only")
        else:
            log.warning("no instruments enabled; run `openchart backfill <INSTRUMENT>` first")
        self._tasks.append(asyncio.create_task(self._status_loop(), name="status"))

    async def _run_feed(self) -> None:
        assert self.feed is not None
        while True:
            try:
                await self.feed.run()
                return
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                self.feed_error = str(exc)
                log.error("feed stopped: %s", exc)
                self.hub.broadcast_status()
                return
            except Exception as exc:
                # For example the network is down at boot and the first gap-fill fails.
                self.feed_error = f"{type(exc).__name__}: {exc}"
                log.exception("feed failed, retrying in %.0fs", FEED_RETRY_SECONDS)
                self.hub.broadcast_status()
                await asyncio.sleep(FEED_RETRY_SECONDS)

    def _on_stream_state(self, state: StreamState, detail: str) -> None:
        if state is StreamState.CONNECTED:
            self.feed_error = None
        self.hub.broadcast_status()

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            self.hub.broadcast_status()

    def status(self) -> dict[str, Any]:
        if self.feed is None:
            state = "not_configured" if self.provider is None else "no_instruments"
            return {"provider": self.source, "provider_state": state, "instruments": {}}
        status = self.feed.status()
        status["error"] = self.feed_error
        return status

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self.provider is not None:
            await self.provider.aclose()
        await self.db.close()
