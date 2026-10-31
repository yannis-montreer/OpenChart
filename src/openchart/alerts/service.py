"""The alert service: runs the engine inside the server (SPEC section 9).

It loads alerts and lines, feeds live ticks to the engine, saves and broadcasts fires,
delivers notifications, runs the catch-up after a gap (with history repaired first),
reports freezes of the server, and runs the watchdog. Independent of any open browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openchart.alerts.engine import AlertEngine, Fire
from openchart.alerts.notifiers.telegram import TelegramClient
from openchart.alerts.notify import (
    Dispatcher,
    Notifier,
    TelegramNotifier,
    alert_text,
    fmt_time,
    zone,
)
from openchart.alerts.watchdog import FeedView, Watchdog, fmt_duration
from openchart.market.sessions import trading_clock
from openchart.store.alerts import Alert, AlertEvent, AlertStore
from openchart.store.drawings import Drawing, DrawingStore

if TYPE_CHECKING:
    from openchart.server import Server

log = logging.getLogger(__name__)

SAVE_INTERVAL = 30.0
WATCHDOG_INTERVAL = 15.0
FREEZE_REPORT_WAIT = 30.0  # wait this long for the catch-up before reporting a freeze
LAST_EVAL_KEY = "alerts_last_eval"


@dataclass
class Freeze:
    start: float
    end: float
    noticed: float
    late: int = 0
    caught_up: bool = False


class AlertService:
    def __init__(self, server: Server, *, now=time.time):
        self.server = server
        self.now = now
        self.store = AlertStore(server.db.conn)
        self.engine = AlertEngine(now=now)
        self.dispatcher = Dispatcher(self.store, self._notifier)
        self.dispatcher.on_delivery = self._delivery_changed
        self.watchdog = Watchdog(now=now)
        self.freeze: Freeze | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._pending: set[asyncio.Future[None]] = set()

    # Lifecycle

    async def start(self) -> None:
        alerts = await self.store.list()
        drawings = await DrawingStore(self.server.db.conn).list()
        self.engine.load(alerts, drawings)
        saved = await self.server.settings.get(LAST_EVAL_KEY, {})
        self.engine.last_eval.update({k: float(v) for k, v in (saved or {}).items()})
        await self.refresh_clocks()
        self.watchdog.reset()
        self._tasks = [
            asyncio.create_task(self._fires(), name="alert-fires"),
            asyncio.create_task(self._loop(), name="alert-loop"),
        ]

    async def announce_start(self) -> None:
        active = self.engine.active()
        instruments = sorted({a.instrument for a in active}) or ["no instrument"]
        self.dispatcher.system(
            f"▶️ OpenChart alert engine started: watching {len(active)} active "
            f"alert{'s' if len(active) != 1 else ''} ({', '.join(instruments)}). "
            "The watchdog reports a stalled price feed."
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self.save_state()
        await self.dispatcher.close()

    async def refresh_clocks(self, instrument: str | None = None) -> None:
        """Trading clocks (hours and learned closures) for the line geometry."""
        source = self.server.source
        for row in await self.server.store.instruments(source):
            if instrument and row.instrument != instrument:
                continue
            closures = await self.server.store.closures(source, row.instrument)
            self.engine.set_clock(row.instrument, trading_clock(row.hours, closures))

    def attach(self, feed) -> None:
        """Connect a (new) feed: live ticks and wake-up notices."""
        feed.tick_listeners.append(self.engine.on_tick)
        feed.on_wake = self.on_wake

    # Changes from the API

    def put(self, alert: Alert, *, rearm: bool = True) -> None:
        self.engine.put(alert, rearm=rearm)
        self.broadcast("update", alert)

    def removed(self, alert: Alert) -> None:
        self.engine.remove(alert.id)
        self.broadcast("delete", alert)

    def drawing_changed(self, drawing: Drawing) -> None:
        self.engine.drawing_changed(drawing)

    async def drawing_deleted(self, drawing_id: str) -> None:
        """Alerts on a deleted line go with it."""
        self.engine.drawing_removed(drawing_id)
        for alert in await self.store.for_drawing(drawing_id):
            await self.store.delete(alert.id)
            self.removed(alert)

    def instrument_deleted(
        self, instrument: str, drawings: list[Drawing], alerts: list[Alert]
    ) -> None:
        """An instrument's data was deleted (`Server.delete_instrument`): its deleted lines
        and alerts leave the engine and the pages, and so do its clock and any catch-up
        still waiting for it (there is nothing left to replay)."""
        for alert in alerts:
            self.removed(alert)
        for drawing in drawings:
            self.engine.drawing_removed(drawing.id)
            self.server.hub.broadcast(
                {"type": "drawing_changed", "op": "delete", "drawing": drawing.to_json()}
            )
        self.engine.gaps.pop(instrument, None)
        self.engine.clocks.pop(instrument, None)

    def broadcast(self, op: str, alert: Alert) -> None:
        self.server.hub.broadcast({"type": "alert_changed", "op": op, "alert": alert.to_json()})

    # Fires

    async def _fires(self) -> None:
        while True:
            fire = await self.engine.fires.get()
            try:
                await self.handle(fire)
            except Exception:
                log.exception("handling an alert fire failed")

    async def handle(self, fire: Fire) -> AlertEvent:
        alert = fire.alert
        now = self.now()
        event = AlertEvent(
            id=0,
            alert_id=alert.id,
            ts=int(fire.ts),
            price=fire.price,
            line_value=fire.line_value,
            cross=fire.cross,
            message="",
            late=fire.late,
            detected_at=int(now),
        )
        text = alert_text(alert, event, await self._decimals(alert.instrument), await self._zone())
        event.message = text
        telegram = "telegram" in alert.notify
        event.delivered = not telegram  # nothing to send
        # The event and the state right after the fire in one step: a crash leaves both
        # or neither (then the catch-up after the restart finds the cross again).
        state = fire.state if fire.state is not None else alert.state
        event = await self.store.record_fire(
            event, alert, state, fire.fire_count, fire.last_fired_at
        )
        self.engine.saved(fire)
        log.info(
            "alert %s fired: %s %s at %s%s",
            alert.id,
            alert.instrument,
            fire.cross,
            fire.price,
            f" (late: {fire.late})" if fire.late else "",
        )
        self.server.hub.broadcast(
            {"type": "alert_triggered", "alert": alert.to_json(), "event": event.to_json()}
        )
        self.broadcast("update", alert)
        if telegram:
            self.dispatcher.event(event, text)
        return event

    def _delivery_changed(self, event_id: int) -> None:
        async def send() -> None:
            event = await self.store.event(event_id)
            if event is not None:
                self.server.hub.broadcast({"type": "alert_event", "event": event.to_json()})

        task = asyncio.ensure_future(send())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _decimals(self, instrument: str) -> int:
        row = await self.server.store.instrument(self.server.source, instrument)
        return row.decimals if row else 5

    async def _zone(self):
        return zone((await self.server.app_settings()).timezone)

    async def _notifier(self) -> Notifier | None:
        secrets = self.server.secrets
        token = secrets.get("telegram_token") if secrets else None
        chat = (await self.server.app_settings()).telegram_chat_id
        if not token or not chat:
            return None
        return TelegramNotifier(
            TelegramClient(token, chat, transport=self.server.telegram_transport)
        )

    # Freeze, catch-up, expiry, watchdog, saving

    def on_wake(self, frozen_from: float, frozen_to: float) -> None:
        """The feed noticed the process was frozen (the computer slept)."""
        self.engine.next_reason = "wake"
        self.watchdog.reset()
        self.freeze = Freeze(frozen_from, frozen_to, self.now())

    async def _loop(self) -> None:
        last_save = last_watch = self.now()
        while True:
            await asyncio.sleep(1)
            now = self.now()
            try:
                await self.step(now)
                if now - last_save >= SAVE_INTERVAL:
                    last_save = now
                    await self.save_state()
                if now - last_watch >= WATCHDOG_INTERVAL:
                    last_watch = now
                    self.watch()
            except Exception:
                log.exception("alert loop step failed")

    async def step(self, now: float) -> None:
        self.engine.on_clock(now)
        while self.engine.changed:
            alert = self.engine.changed.pop(0)
            await self.store.save(alert, touch=False)
            self.broadcast("update", alert)
            if alert.status == "expired" and "telegram" in alert.notify:
                self.dispatcher.system(
                    f"⌛ Alert expired: {alert.name or alert.id}. It no longer watches"
                    " the price; restart it or change its expiration in OpenChart."
                )
        for instrument in list(self.engine.gaps):
            await self.catch_up(instrument)
        await self._report_freeze(now)

    async def catch_up(self, instrument: str) -> int:
        gap = self.engine.gaps[instrument]
        feed = self.server.feed
        # Only an instrument the feed serves: a removed one is not downloaded into again
        # (and may be being deleted). Under its lock, so a delete refuses meanwhile.
        if feed is not None and instrument in feed.instruments:
            async with self.server.instrument_locks.hold(
                instrument, "repair before the alert catch-up"
            ):
                try:  # the missed minutes must be stored before they can be replayed
                    await feed.repair_instrument(instrument)
                except Exception as exc:
                    log.warning(
                        "repairing %s before the alert catch-up failed: %s", instrument, exc
                    )
        start, end = self.engine.catch_up_range(instrument) or (0, 0)
        candles = (
            await self.server.store.read_m1(self.server.source, instrument, start, end)
            if end > start
            else []
        )
        late = self.engine.catch_up(instrument, candles)
        log.info(
            "%s: alert catch-up over %d minutes (%s): %d fired late",
            instrument,
            len(candles),
            gap.reason,
            late,
        )
        if gap.reason == "wake" and self.freeze is not None:
            self.freeze.late += late
            self.freeze.caught_up = True
        return late

    async def _report_freeze(self, now: float) -> None:
        f = self.freeze
        if f is None or any(g.reason == "wake" for g in self.engine.gaps.values()):
            return
        if not f.caught_up and now - f.noticed < FREEZE_REPORT_WAIT:
            return
        self.freeze = None
        if self.engine.next_reason == "wake":
            self.engine.next_reason = "reconnect"
        tz = await self._zone()
        fired = (
            f"{f.late} alert{'s' if f.late != 1 else ''} fired late, marked "
            '"detected after wake-up"'
            if f.late
            else "none fired"
        )
        self.dispatcher.system(
            f"⏸️ OpenChart was frozen from {fmt_time(f.start, tz)} to {fmt_time(f.end, tz)} "
            f"({fmt_duration(f.end - f.start)}): the computer slept or the process was "
            "paused. Prices for that time were repaired and the alerts checked against "
            f"them: {fired}. Alerts from that period may have been late."
        )

    def watch(self) -> None:
        feed = self.server.feed
        if feed is None:
            return
        now = self.now()
        views = {}
        for instrument in feed.instruments:
            clock = self.engine.clocks.get(instrument)
            market_open = clock is None or clock(now + 60) > clock(now)
            tick = feed.last_tick.get(instrument)
            views[instrument] = FeedView(tick.ts if tick else None, market_open)
        state = feed.stream.state.value + (
            f" ({self.server.feed_error})" if self.server.feed_error else ""
        )
        for message in self.watchdog.check(views, state):
            log.warning(message)
            self.dispatcher.system(message)

    async def save_state(self) -> None:
        """The regular save. Fires not written yet hold back their alerts' states and the
        `last_eval` of their instrument (`AlertEngine.holding`), so that a crash before
        they are written leaves a state from before them, and a restart replays them."""
        held, before = self.engine.holding()
        engine = self.engine
        dirty = [engine.alerts[i] for i in engine.dirty if i in engine.alerts and i not in held]
        engine.dirty &= held
        if dirty:
            await self.store.save_states(dirty)
        if engine.last_eval:
            last = {k: min(v, before.get(k, v)) for k, v in engine.last_eval.items()}
            await self.server.settings.set(LAST_EVAL_KEY, last)
