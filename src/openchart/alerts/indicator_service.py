"""Indicator alerts in the server: the evaluator (`indicator_eval`) wired to the stored
bars, the built-in indicators (computed here, in a thread) and your files (the isolated
worker, at the alerts' priority), and its results written, broadcast and sent.

`AlertService` owns one `IndicatorAlerts`. It runs its own loop, so a slow indicator
never delays the line alerts' loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import pandas as pd

from openchart.alerts.engine import CATCH_UP_MIN_SECONDS
from openchart.alerts.indicator_eval import (
    COLUMNS,
    GroupKey,
    IndicatorEvaluator,
    Notice,
    Resolved,
)
from openchart.alerts.notify import indicator_text
from openchart.indicators._base import Indicator, Info
from openchart.market.series import MAX_LIMIT
from openchart.market.timeframes import Timeframe, parse_timeframe
from openchart.store.alerts import Alert, AlertEvent

if TYPE_CHECKING:
    from openchart.alerts.service import AlertService

log = logging.getLogger(__name__)

LOOP_SECONDS = 1.0


class ServerDefinitions:
    """Definitions as they are now: built-ins from the registry, your files from the
    folder (read again when they change)."""

    def __init__(self, server):
        self.server = server
        self._warmups: dict[tuple[str, str, str], int] = {}

    async def resolve(self, kind: str, indicator_id: str) -> Resolved:
        if kind == "builtin":
            ind = self.server.indicators.get(indicator_id)
            return Resolved(ind, "builtin", "ok" if ind is not None else "missing")
        d = await self.server.custom.get(indicator_id)
        if d is None:
            return Resolved(None, None, "missing", f"no file {indicator_id}.py")
        if d.status == "quarantined":
            return Resolved(None, d.digest, d.status, f"quarantined: {d.reason}")
        if d.indicator is None:
            return Resolved(None, d.digest, d.status, d.reason or d.status)
        return Resolved(d.indicator, d.digest, "ok")

    async def warmup(self, key: GroupKey, schema: Indicator, settings: dict[str, Any]) -> int:
        if key.kind == "builtin":
            return int(schema.warmup(settings))
        memo = (key.id, key.digest, key.params)
        if memo not in self._warmups:
            d = await self.server.custom.get(key.id)
            if d is None:
                raise RuntimeError(f"no file {key.id}.py")
            self._warmups[memo] = int(await self.server.custom.warmup(d, settings))
            if len(self._warmups) > 512:
                self._warmups.pop(next(iter(self._warmups)))
        return self._warmups[memo]

    def checks(self, key: GroupKey, settings: dict[str, Any]) -> dict[str, Any] | None:
        d = self.server.custom._defs.get(key.id)
        if d is None or d.digest != key.digest:
            return None
        return self.server.custom.checks_for(d, settings)


class ServerBars:
    """Closed bars from the store (the read path of `/api/candles`, without the forming
    bar), with `warm` bars before the first one."""

    def __init__(self, server):
        self.server = server

    async def read(
        self,
        instrument: str,
        tf: Timeframe,
        price: str,
        start: int | None,
        end: int,
        warm: int,
        now: float,
    ) -> pd.DataFrame:
        read = self.server.reader.candles
        source = self.server.source
        cols = [*COLUMNS, "complete"]
        if start is None:
            frame = await read(
                source, instrument, tf, price=price, end=end, limit=max(warm, 1), now=now
            )
            return frame[cols].reset_index(drop=True)
        pages = []
        at = start
        while True:
            page = await read(source, instrument, tf, price=price, start=at, end=end, now=now)
            if page.empty:
                break
            pages.append(page[cols])
            if len(page) < MAX_LIMIT:
                break
            at = int(page["ts"].iloc[-1]) + 1
        frame = pd.concat(pages, ignore_index=True) if pages else pd.DataFrame(columns=cols)
        before: list[pd.DataFrame] = []
        first, need = (int(frame["ts"].iloc[0]) if not frame.empty else start), warm
        while need > 0:
            older = await read(
                source, instrument, tf, price=price, end=first, limit=min(need, MAX_LIMIT), now=now
            )
            if older.empty:
                break
            before.insert(0, older[cols])
            first, need = int(older["ts"].iloc[0]), need - len(older)
        return pd.concat([*before, frame], ignore_index=True) if before else frame


class IndicatorAlerts:
    def __init__(self, service: AlertService):
        self.service = service
        server = service.server
        self.server = server
        self.evaluator = IndicatorEvaluator(
            store=service.store,
            alerts=lambda: list(service.engine.alerts.values()),
            definitions=ServerDefinitions(server),
            bars=ServerBars(server),
            compute=self._compute,
            now=service.now,
            fresh=self._fresh,
            market_open=self._market_open,
            wake=lambda: service.freeze is not None or service.engine.next_reason == "wake",
            text=self._text,
            traded=self._traded,
        )
        self._task: asyncio.Task[None] | None = None

    # The loop ------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="indicator-alerts")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_SECONDS)
            try:
                await self.run_once()
            except Exception:
                log.exception("indicator alerts: a run failed")

    async def run_once(self, now: float | None = None) -> Notice:
        notice = await self.evaluator.step(now)
        self.deliver(notice)
        return notice

    # What the evaluator needs from the server ----------------------------------------

    def _fresh(self, instrument: str, end: int) -> bool:
        """Prices of `instrument` are known up to `end`: the line engine observed (or
        replayed after a gap) a price at or after it, or no minute of trading time lies
        between its last observation and `end` (a quiet minute, the weekend)."""
        engine = self.service.engine
        if instrument in engine.gaps:
            return False  # waiting for the repair and the catch-up
        last = engine.last_eval.get(instrument)
        if last is None:
            return False
        if last >= end:
            return True
        clock = engine.clocks.get(instrument)
        if clock is None:
            return False
        return clock(end) - clock(last) < min(60, CATCH_UP_MIN_SECONDS)

    def _traded(self, instrument: str, start: int, end: int) -> bool:
        clock = self.service.engine.clocks.get(instrument)
        return clock is None or clock(end) > clock(start)

    def _market_open(self, instrument: str, now: float) -> bool:
        clock = self.service.engine.clocks.get(instrument)
        return clock is None or clock(now + 60) > clock(now)

    async def _info(self, key: GroupKey) -> Info:
        row = await self.server.store.instrument(self.server.source, key.instrument)
        return Info(
            instrument=key.instrument,
            display_name=row.display_name if row else key.instrument,
            decimals=row.decimals if row else 5,
            timeframe=parse_timeframe(key.timeframe).name,
            price=key.price,
        )

    async def _compute(
        self, key: GroupKey, schema: Indicator, settings: dict[str, Any], frame: pd.DataFrame
    ) -> dict[str, pd.Series]:
        info = await self._info(key)
        if key.kind == "builtin":
            return await asyncio.to_thread(schema.run, frame, settings, info)
        d = await self.server.custom.get(key.id)
        if d is None:
            raise RuntimeError(f"no file {key.id}.py")
        return await self.server.custom.compute(d, frame, settings, info, priority="alert")

    async def _text(self, alert: Alert, event: AlertEvent) -> str:
        decimals = await self.service._decimals(alert.instrument)
        return indicator_text(alert, event, decimals, await self.service._zone())

    # Results -------------------------------------------------------------------------

    def deliver(self, notice: Notice) -> None:
        seen: set[str] = set()
        for alert in notice.changed:
            if alert.id not in seen:
                seen.add(alert.id)
                self.service.broadcast("update", alert)
        hub = self.server.hub
        for fire in notice.fires:
            event = fire.event
            assert event is not None
            hub.broadcast(
                {"type": "alert_triggered", "alert": fire.alert.to_json(), "event": event.to_json()}
            )
            if "telegram" in fire.alert.notify:
                self.service.dispatcher.event(event, event.message)
        for event in notice.corrected:
            hub.broadcast({"type": "alert_event", "event": event.to_json()})
        for text in notice.messages:
            log.warning(text)
            self.service.dispatcher.system(text)

    def touched(self, alert: Alert) -> None:
        if alert.type == "indicator":
            self.evaluator.touched(alert.id)

    async def chart_changed(self, instrument: str, instances: list[Any]) -> None:
        """The chart of `instrument` was written: alerts on removed instances stop, those
        whose values or file changed are re-armed (`IndicatorEvaluator.instances_changed`)."""
        if not any(
            a.type == "indicator" and a.active and a.instrument == instrument
            for a in self.service.engine.alerts.values()
        ):
            return
        from openchart.api.chart_indicators import fit  # the API's fitting, without a cycle

        definitions = self.evaluator.definitions
        now_on_chart: dict[str, tuple[Resolved, dict[str, Any]]] = {}
        for i in instances:
            resolved = await definitions.resolve(i.kind, i.indicator)
            params = dict(i.params)
            if resolved.schema is not None:
                params, _ = fit(resolved.schema, i.params, strict=False)
                style = {p.name for p in resolved.schema.params if p.group == "style"}
                params = {k: v for k, v in params.items() if k not in style}
            now_on_chart[i.uid] = (resolved, params)
        notice = await self.evaluator.instances_changed(instrument, now_on_chart)
        self.deliver(notice)

    def behind(self) -> list[str]:
        return self.evaluator.behind()
