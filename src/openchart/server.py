"""Server lifecycle: database, settings, auth, provider, feed, candle builder and hub.

The feed is (re)built by `reconfigure()` from the saved settings and secrets, so setup,
a token rotation or a new instrument takes effect without restarting the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openchart import retention
from openchart.alerts.service import AlertService
from openchart.api.auth import Auth
from openchart.api.hub import Hub
from openchart.config import Config, OandaConfig
from openchart.indicators._cache import ValueCache
from openchart.indicators._custom import CustomIndicators
from openchart.indicators._registry import Registry, load_indicators
from openchart.indicators.worker.isolation import Isolation, start_worker
from openchart.indicators.worker.runner import IndicatorRunner
from openchart.lock import DataFolderInUse, DataFolderLock, in_use_message
from openchart.market.bars import BarCache
from openchart.market.cache import CandleCache
from openchart.market.candle_builder import CandleBuilder
from openchart.market.feed import Feed
from openchart.market.series import SeriesReader
from openchart.providers.base import AuthError, Provider
from openchart.providers.stream import StreamState
from openchart.settings import (
    SECRET_NAMES,
    AppSettings,
    SecretKeyChanged,
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
RETENTION_CHECK_SECONDS = 60.0  # how often the date is checked: the run is just after 00:00 UTC

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
        indicator_worker: bool = True,
        indicator_launchers: list[Any] | None = None,
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
        self.indicators = Registry()  # built-in: they run here (D5)
        self.custom = CustomIndicators(self.indicators_dir)  # yours: they run in the worker
        self.indicator_runner: IndicatorRunner | None = None
        self.values_cache = ValueCache()  # computed indicator values (CI-5)
        self._indicator_worker = indicator_worker  # tests that need no worker turn it off
        self._indicator_launchers = indicator_launchers
        self.db: Database | None = None
        self.lock: DataFolderLock | None = None
        self.alerts: AlertService | None = None

    @property
    def indicators_dir(self) -> Path:
        """The owner's indicator files (docs/indicators.md)."""
        return self.config.data_dir / "indicators"

    @property
    def source(self) -> str:
        return self.provider.name if self.provider else DEFAULT_SOURCE

    async def start(self) -> None:
        """Open everything and start the feed. If anything fails, what was opened is
        closed again before the error goes up: an open database connection would keep
        the process alive, a server that never answers (found in the M8 Docker test)."""
        try:
            await self._start()
        except BaseException:
            with contextlib.suppress(Exception):
                await self.stop()
            raise

    async def _start(self) -> None:
        cfg = self.config
        lock = DataFolderLock(cfg.data_dir)
        if not lock.acquire():
            # Two servers on one folder would download and alert twice; a command that
            # writes (migrate, repair...) must not run underneath either.
            raise DataFolderInUse(in_use_message(cfg.data_dir, "start the server"))
        self.lock = lock
        self.db = await Database.open(cfg.db_path)
        self.store = CandleStore(self.db.conn)
        self.settings = SettingsStore(self.db.conn)
        self.auth = Auth(self.db.conn, self.settings)
        if self._fixed_secrets is not None:
            self.secrets = self._fixed_secrets
        else:
            try:
                self.secrets = open_secrets(cfg, await self.settings.instance_id())
                # Where the tokens come from, and whether they are there (never their
                # values): the first thing to check when a service cannot connect.
                present = {name: bool(self.secrets.get(name)) for name in SECRET_NAMES}
                log.info(
                    "secrets: %s; %s",
                    self.secrets.backend,
                    ", ".join(f"{n} {'set' if ok else 'not set'}" for n, ok in present.items()),
                )
            except SecretKeyChanged:
                raise  # see SecretKeyChanged: `openchart run` stops with an explanation
            except SecretStoreUnavailable as exc:
                self.secrets_error = str(exc)
                self.secrets = None
                log.error("secrets: %s", exc)

        self.builder = CandleBuilder()
        self.cache = CandleCache(self.store)
        self.hub = Hub(self.builder, status=self.status)
        self.reader = SeriesReader(
            self.store,
            cfg.ticks_dir,
            lambda instrument: self.tick_writer.buffered(instrument) if self.tick_writer else [],
            cache=self.cache,
            live=lambda instrument: self.builder.forming_m1(instrument),
            # The read path only: alerts, repair, downloads and the feed read the store.
            bars=BarCache(self.store),
        )
        with contextlib.suppress(OSError):
            self.indicators_dir.mkdir(parents=True, exist_ok=True)
        self.indicators = load_indicators()
        log.info("built-in indicators: %s", ", ".join(self.indicators.indicators) or "none")
        # In the background: the first worker takes a second or two (importing pandas).
        self._tasks.append(asyncio.create_task(self._start_indicator_worker(), name="worker"))
        self._tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        self.alerts = AlertService(self)
        await self.alerts.start()

        if not await self.auth.password_set():
            log.warning(
                "OpenChart is not set up yet. Open the web page to run the setup wizard. "
                "From another device you will be asked for this setup code: %s",
                self.auth.setup_code,
            )
        await self.reconfigure()
        await self.alerts.announce_start()
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))

    async def _retention_loop(self) -> None:
        """At start and once a UTC day: `run_retention`. A failure is logged and retried
        the next day; it never stops the server."""
        last = None
        while True:
            now = time.time()
            if retention.retention_due(last, now):
                last = retention.utc_day(now)
                try:
                    await self.run_retention(now)
                except Exception:
                    log.exception("retention")
            await asyncio.sleep(RETENTION_CHECK_SECONDS)

    async def run_retention(self, now: float | None = None) -> retention.Report:
        """Delete what is older than the limits (`openchart.retention`): tick days past the
        tick setting, minutes past each enabled instrument's depth, old rotated service
        logs. One log line per deletion; nothing newer than a limit is touched.

        The first time an install runs this (no `retention_since` yet), it only logs what
        would go and deletes nothing for `RETENTION_GRACE_SECONDS`: time to choose other
        limits in Settings, or to see them with `openchart retention`, before anything goes."""
        now = time.time() if now is None else now
        report = retention.Report()
        since = await self.settings.get(retention.SINCE_KEY)
        if since is None:
            since = now
            await self.settings.set(retention.SINCE_KEY, since)
        app = await self.app_settings()
        days = retention.TICK_RETENTION[app.tick_retention]
        rows = await self.store.instruments(self.source, enabled_only=True)
        plans = await retention.plan_m1(
            self.store, self.source, rows, now, self.config.backfill_years
        )
        deletes_ticks = days is not None and self.tick_writer is not None
        if now < since + retention.GRACE_SECONDS:
            ticks = []
            if deletes_ticks:
                ticks = retention.expired_tick_days(self.config.ticks_dir, self.source, now, days)
            log.warning(
                "retention: the limits apply from %s UTC (a day after their first start);"
                " nothing deleted before then. It would delete %d tick day(s) and %s minutes."
                " See `openchart retention`; change the limits in Settings (Data, Instruments).",
                retention.fmt_ts(int(since + retention.GRACE_SECONDS)),
                len(ticks),
                ", ".join(f"{p.instrument} {p.minutes:,}" for p in plans) or "no",
            )
            return report
        if deletes_ticks:
            report.ticks = await asyncio.to_thread(self.tick_writer.delete_expired, now, days)
        for p in plans:
            if not p.minutes:
                continue
            deleted = await self.store.purge_m1_before(self.source, p.instrument, p.cutoff)
            log.info(
                "retention: %s minutes before %s UTC deleted: %d (%s to %s; kept %s)",
                p.instrument,
                retention.fmt_ts(p.cutoff),
                deleted,
                retention.fmt_ts(p.oldest),
                retention.fmt_ts(p.newest),
                p.depth,
            )
            report.m1.append(p)
        report.logs = await asyncio.to_thread(
            retention.prune_service_logs, self.config.data_dir / "logs"
        )
        return report

    @property
    def isolation(self) -> Isolation | None:
        """How custom indicator code is isolated here; None while the worker starts."""
        return self.custom.isolation

    async def _start_indicator_worker(self) -> None:
        if not self._indicator_worker:
            self.custom.worker_ready(
                None, Isolation("unavailable", False, "the indicator worker is turned off")
            )
            return
        try:
            runner, isolation = await start_worker(
                self.config.data_dir, launchers=self._indicator_launchers
            )
        except Exception as exc:  # never takes the server down
            log.exception("indicator worker")
            runner, isolation = (
                None,
                Isolation("unavailable", False, f"{type(exc).__name__}: {exc}"),
            )
        self.indicator_runner = runner
        self.custom.worker_ready(runner, isolation)
        if runner is None:
            log.error("indicator sandbox: unavailable: %s", isolation.reason)

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
                    m1_depth_days={
                        r.instrument: retention.m1_depth_days(r.m1_depth, cfg.backfill_years)
                        for r in rows
                    },
                )
                self.feed.tick_listeners.append(self.hub.on_tick)
                self.feed.on_closures = self.broadcast_closures
                self.alerts.attach(self.feed)
                self.feed.stream.on_state = self._on_stream_state
                await self.feed.find_downloads()  # so the first status already shows them
                if self.run_feed:
                    self._feed_task = asyncio.create_task(self._run_feed(self.feed), name="feed")
            await self.alerts.refresh_clocks()
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
        await self.alerts.refresh_clocks(instrument)
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
        """Also safe after a start that failed half way."""
        await self._stop_feed()
        if self.alerts is not None:
            await self.alerts.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self.custom.close()
        if self.indicator_runner is not None:
            await self.indicator_runner.stop()
            self.indicator_runner = None
        if self._fixed_provider is not None:
            await self._fixed_provider.aclose()
        if self.db is not None:
            await self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.release()
            self.lock = None
