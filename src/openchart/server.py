"""Server lifecycle: database, settings, auth, provider, feed, candle builder and hub.

The feed is (re)built by `reconfigure()` from the saved settings and secrets, so setup,
a token rotation or a new instrument takes effect without restarting the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from openchart.api.auth import Auth
from openchart.api.hub import Hub
from openchart.config import Config, OandaConfig
from openchart.market.cache import CandleCache
from openchart.market.candle_builder import CandleBuilder
from openchart.market.feed import Feed
from openchart.market.series import SeriesReader
from openchart.providers.base import AuthError, Provider
from openchart.providers.stream import StreamState
from openchart.settings import (
    AppSettings,
    Secrets,
    SecretStoreUnavailable,
    SettingsStore,
    effective_config,
    load_app_settings,
    open_secrets,
)
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from openchart.store.ticks import TickWriter

log = logging.getLogger(__name__)

DEFAULT_SOURCE = "oanda"
FEED_RETRY_SECONDS = 30.0
STATUS_INTERVAL = 5.0

ProviderFactory = Callable[[OandaConfig], Provider]


def oanda_factory(oanda: OandaConfig) -> Provider:
    from openchart.providers.oanda import OandaProvider

    return OandaProvider(oanda)


class Server:
    def __init__(
        self,
        config: Config,
        provider: Provider | None = None,
        run_feed: bool = True,
        *,
        secrets: Secrets | None = None,
        provider_factory: ProviderFactory = oanda_factory,
    ):
        self.config = config
        self._fixed_provider = provider  # tests inject one; it is then always used
        self._fixed_secrets = secrets
        self.provider_factory = provider_factory
        self.run_feed = run_feed
        self.provider: Provider | None = None
        self.feed: Feed | None = None
        self.tick_writer: TickWriter | None = None
        self.feed_error: str | None = None
        self.secrets: Secrets | None = None
        self.secrets_error: str | None = None
        self._feed_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._reconfigure_lock = asyncio.Lock()
        self.telegram_transport = None  # tests replace the HTTP transport

    @property
    def source(self) -> str:
        return self.provider.name if self.provider else DEFAULT_SOURCE

    async def start(self) -> None:
        cfg = self.config
        self.db = await Database.open(cfg.db_path)
        self.store = CandleStore(self.db.conn)
        self.settings = SettingsStore(self.db.conn)
        self.auth = Auth(self.db.conn, self.settings)
        if self._fixed_secrets is not None:
            self.secrets = self._fixed_secrets
        else:
            try:
                self.secrets = open_secrets(cfg, await self.settings.instance_id())
            except SecretStoreUnavailable as exc:
                self.secrets_error = str(exc)
                log.error("secrets: %s", exc)

        self.builder = CandleBuilder()
        self.cache = CandleCache(self.store)
        self.hub = Hub(self.builder, status=self.status)
        self.reader = SeriesReader(
            self.store,
            cfg.ticks_dir,
            lambda instrument: self.tick_writer.buffered(instrument) if self.tick_writer else [],
            cache=self.cache,
        )
        self._tasks.append(asyncio.create_task(self._status_loop(), name="status"))

        if not await self.auth.password_set():
            log.warning(
                "OpenChart is not set up yet. Open the web page to run the setup wizard. "
                "From another device you will be asked for this setup code: %s",
                self.auth.setup_code,
            )
        await self.reconfigure()

    async def app_settings(self) -> AppSettings:
        return await load_app_settings(self.settings, self.config)

    async def effective_config(self) -> Config:
        return effective_config(self.config, await self.app_settings(), self.secrets)

    async def reconfigure(self) -> None:
        """Rebuild provider and feed from the current settings and secrets."""
        async with self._reconfigure_lock:
            await self._stop_feed()
            cfg = await self.effective_config()
            if self._fixed_provider is not None:
                self.provider = self._fixed_provider
            elif cfg.oanda.configured:
                self.provider = self.provider_factory(cfg.oanda)
            else:
                self.provider = None
            self.feed_error = None
            self.tick_writer = TickWriter(cfg.ticks_dir, self.source) if cfg.store_ticks else None

            rows = await self.store.instruments(self.source, enabled_only=True)
            if self.provider is None:
                log.warning("no broker credentials yet; serving stored data only")
            elif not rows:
                log.warning("no instruments enabled yet")
            else:
                self.feed = Feed(
                    self.provider,
                    self.store,
                    [r.instrument for r in rows],
                    hours={r.instrument: r.hours for r in rows},
                    tick_writer=self.tick_writer,
                    builder=self.builder,
                    cache=self.cache,
                    deep_history=cfg.deep_history,
                    backfill_years=cfg.backfill_years,
                )
                self.feed.tick_listeners.append(self.hub.on_tick)
                self.feed.on_closures = self.broadcast_closures
                self.feed.stream.on_state = self._on_stream_state
                if self.run_feed:
                    self._feed_task = asyncio.create_task(self._run_feed(self.feed), name="feed")
            self.hub.broadcast_status()

    async def _stop_feed(self) -> None:
        if self._feed_task is not None:
            self._feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._feed_task
            self._feed_task = None
        self.feed = None
        if self.provider is not None and self.provider is not self._fixed_provider:
            await self.provider.aclose()
        self.provider = None

    async def _run_feed(self, feed: Feed) -> None:
        while True:
            try:
                await feed.run()
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

    async def broadcast_closures(self, instrument: str) -> None:
        """Browsers redraw lines with the instrument's newly learned closures."""
        closures = await self.store.closures(self.source, instrument)
        self.hub.broadcast(
            {"type": "closures", "instrument": instrument, "closures": [list(c) for c in closures]}
        )

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
            return {
                "provider": self.source,
                "provider_state": state,
                "instruments": {},
                "error": self.feed_error,
            }
        status = self.feed.status()
        status["error"] = self.feed_error
        return status

    async def stop(self) -> None:
        await self._stop_feed()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._fixed_provider is not None:
            await self._fixed_provider.aclose()
        await self.db.close()
