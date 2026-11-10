"""Alert routes (SPEC sections 7 and 9). Changes are broadcast as `alert_changed`.

Indicator alerts (`type: "indicator"`, docs/designs/indicator-alerts.md) go through the
same routes, with `indicator: {uid, output, condition, other_output?, value?}`; the
snapshot and its checks are in `api/indicator_alerts.py`. Line and level alerts take and
answer exactly what they did before (`tests/test_line_alerts_unchanged.py`).
"""

from __future__ import annotations

import math
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from openchart.alerts import indicator_rules
from openchart.alerts.notify import default_name
from openchart.api.indicator_alerts import IndicatorIn, snapshot
from openchart.drawing_rules import LINE_TYPES
from openchart.market.timeframes import Kind, parse_timeframe
from openchart.server import Server
from openchart.store.alerts import Alert, IndicatorState
from openchart.store.drawings import DrawingStore

AlertType = Literal["line_cross", "level_cross", "indicator"]
Direction = Literal["cross", "up", "down"]
PriceKind = Literal["bid", "ask", "mid"]
Mode = Literal["touch", "candle_close"]
Frequency = Literal["once", "once_per_bar", "repeating"]
Channel = Literal["telegram", "app"]
STOPPED_BY_YOU = "stopped by you"


def _timeframe(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        tf = parse_timeframe(value)
    except (KeyError, ValueError):
        raise ValueError(f"unknown timeframe {value!r}") from None
    if tf.kind is Kind.TICK:
        raise ValueError("alerts need a timeframe of 1m or more")
    return tf.name


class AlertFields(BaseModel):
    direction: Direction | None = None
    trigger_price: PriceKind | None = None
    trigger_mode: Mode | None = None
    timeframe: str | None = None
    frequency: Frequency | None = None
    cooldown_s: int | None = Field(None, ge=0, le=7 * 86400)
    level: float | None = None
    name: str | None = Field(None, max_length=200)
    expires_at: int | None = None
    notify: list[Channel] | None = None
    indicator: IndicatorIn | None = None  # indicator alerts only

    @field_validator("timeframe")
    @classmethod
    def valid_timeframe(cls, v: str | None) -> str | None:
        return _timeframe(v)

    @field_validator("level")
    @classmethod
    def finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("the level must be a finite number")
        return v


class AlertCreate(AlertFields):
    instrument: str
    type: AlertType
    drawing_id: str | None = None


class AlertUpdate(AlertFields):
    status: Literal["active", "stopped"] | None = None
    clear_expiry: bool = False


# Fields whose change makes the alert watch something else: it is re-armed.
CONDITION_FIELDS = ("direction", "trigger_price", "trigger_mode", "timeframe", "level")


def alert_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api/alerts")

    def service():
        return server.alerts

    async def line_type(alert: Alert) -> str | None:
        if alert.drawing_id is None:
            return None
        drawing = await DrawingStore(server.db.conn).get(alert.drawing_id)
        return drawing.type if drawing else None

    @router.get("")
    async def list_alerts(
        instrument: str | None = None, drawing_id: str | None = None, uid: str | None = None
    ) -> list[dict[str, Any]]:
        alerts = await service().store.list(instrument)
        if drawing_id is not None:
            alerts = [a for a in alerts if a.drawing_id == drawing_id]
        if uid is not None:  # the alerts on one indicator instance (its legend row)
            alerts = [a for a in alerts if a.indicator is not None and a.indicator.uid == uid]
        return [a.to_json() for a in alerts]

    @router.get("/events")
    async def events(limit: int = 100, alert_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        return [e.to_json() for e in await service().store.events(limit, alert_id)]

    @router.post("")
    async def create(body: AlertCreate) -> dict[str, Any]:
        row = await server.store.instrument(server.source, body.instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {body.instrument!r}")
        if body.type != "indicator" and body.indicator is not None:
            raise HTTPException(400, "only an indicator alert takes 'indicator'")
        alert = Alert(id="", instrument=body.instrument, type=body.type)
        if body.type == "indicator":
            await _indicator_create(alert, body)
        elif body.type == "line_cross":
            drawing = await DrawingStore(server.db.conn).get(body.drawing_id or "")
            if (
                drawing is None
                or drawing.instrument != body.instrument
                or drawing.type not in LINE_TYPES
            ):
                raise HTTPException(
                    400, "a line alert needs a line on this instrument (not another drawing)"
                )
            alert.drawing_id = drawing.id
        elif body.level is None:
            raise HTTPException(400, "a level alert needs a price level")
        _apply(alert, body)
        if alert.expires_at is not None and alert.expires_at <= time.time():
            raise HTTPException(400, "the expiration is in the past")
        if not alert.name:
            if alert.indicator is not None:
                alert.name = indicator_rules.default_name(
                    alert.instrument, alert.timeframe, alert.direction, alert.indicator
                )
            else:
                alert.name = default_name(alert, await line_type(alert), row.decimals)
        alert = await service().store.create(alert)
        service().put(alert)
        return alert.to_json()

    async def _indicator_create(alert: Alert, body: AlertCreate) -> None:
        if body.indicator is None:
            raise HTTPException(400, "an indicator alert needs 'indicator'")
        if body.drawing_id is not None or body.level is not None:
            raise HTTPException(400, "an indicator alert takes no drawing_id and no level")
        if body.timeframe is None:
            raise HTTPException(400, "an indicator alert needs a timeframe")
        _indicator_fields(body, body.indicator.condition)
        alert.indicator, warnings = await snapshot(server, body.instrument, body.indicator)
        alert.trigger_mode = "candle_close"
        alert.istate = IndicatorState(warnings=warnings)

    @router.put("/{alert_id}")
    async def update(alert_id: str, body: AlertUpdate) -> dict[str, Any]:
        alert = service().engine.alerts.get(alert_id) or await service().store.get(alert_id)
        if alert is None:
            raise HTTPException(404, "no such alert")
        if alert.type == "indicator":
            return await _indicator_update(alert, body)
        if body.indicator is not None:
            raise HTTPException(400, "only an indicator alert takes 'indicator'")
        if alert.type == "level_cross" and "level" in body.model_fields_set and body.level is None:
            raise HTTPException(400, "a level alert needs a price level")
        before = {f: getattr(alert, f) for f in CONDITION_FIELDS}
        was_active = alert.active
        _apply(alert, body)
        if body.clear_expiry:
            alert.expires_at = None
        if body.status is not None:
            alert.status = body.status
        if alert.active and alert.expires_at is not None and alert.expires_at <= time.time():
            raise HTTPException(400, "the expiration is in the past")
        rearm = before != {f: getattr(alert, f) for f in CONDITION_FIELDS} or (
            alert.active and not was_active
        )
        await service().store.save(alert)
        service().put(alert, rearm=rearm)
        return alert.to_json()

    async def _indicator_update(alert: Alert, body: AlertUpdate) -> dict[str, Any]:
        """An edit of an indicator alert. A new condition, timeframe, price type or
        direction, or starting it again, takes a new snapshot of the instance and re-arms
        it: its first evaluation after records the state and never fires."""
        if alert.indicator is None:
            raise HTTPException(409, "this alert's condition is incomplete: delete it")
        if "level" in body.model_fields_set:
            raise HTTPException(400, "an indicator alert takes no level")
        given = body.indicator
        if given is not None and given.uid != alert.indicator.uid:
            raise HTTPException(
                400, "an alert stays on its indicator: create a new alert for another one"
            )
        current = alert.indicator
        condition = given.condition if given is not None else current.condition
        _indicator_fields(body, condition)
        if body.expires_at is not None or body.clear_expiry:
            expires = None if body.clear_expiry else body.expires_at
        else:
            expires = alert.expires_at
        status = body.status or alert.status
        if status == "active" and expires is not None and expires <= time.time():
            raise HTTPException(400, "the expiration is in the past")
        before = (
            {f: getattr(alert, f) for f in CONDITION_FIELDS},
            current.output,
            current.condition,
            current.other_output,
            current.value,
        )
        after_fields = {
            f: (getattr(body, f) if getattr(body, f) is not None else getattr(alert, f))
            for f in CONDITION_FIELDS
        }
        after_fields["trigger_mode"] = "candle_close"
        if condition == "signal":
            after_fields["direction"] = "cross"
        after = (
            after_fields,
            *(
                (given.output, given.condition, given.other_output, given.value)
                if given is not None
                else (current.output, current.condition, current.other_output, current.value)
            ),
        )
        starting = status == "active" and not alert.active
        rearm = before != after or starting
        cond, warnings = current, alert.istate.warnings
        if given is not None or starting:  # checked against the chart and file as they are now
            wanted = given or IndicatorIn(
                uid=current.uid,
                output=current.output,
                condition=current.condition,  # type: ignore[arg-type]
                other_output=current.other_output,
                value=current.value,
            )
            cond, warnings = await snapshot(server, alert.instrument, wanted)
        # Everything is checked: apply.
        _apply(alert, body)
        alert.trigger_mode = "candle_close"
        if condition == "signal":
            alert.direction = "cross"
        alert.expires_at = expires
        alert.indicator = cond
        if body.status == "stopped" and alert.status != "stopped":
            alert.stop_reason = STOPPED_BY_YOU
        alert.status = status
        if alert.active:
            alert.stop_reason = None
        if rearm:
            alert.istate = IndicatorState(warnings=list(warnings))
        await service().store.save(alert)
        service().put(alert, rearm=rearm)
        return alert.to_json()

    @router.delete("/{alert_id}")
    async def delete(alert_id: str) -> dict[str, Any]:
        alert = await service().store.delete(alert_id)
        if alert is None:
            raise HTTPException(404, "no such alert")
        service().removed(alert)
        return alert.to_json()

    return router


def _indicator_fields(body: AlertFields, condition: str) -> None:
    """The fields an indicator alert refuses: it is evaluated on closed bars only (owner's
    default), and a signal has no direction."""
    if body.trigger_mode == "touch":
        raise HTTPException(
            400, "an indicator alert is evaluated on closed bars only (candle_close)"
        )
    if condition == "signal" and body.direction not in (None, "cross"):
        raise HTTPException(400, "a signal alert has no direction (leave it 'cross')")


def _apply(alert: Alert, body: AlertFields) -> None:
    for name in (
        "direction",
        "trigger_price",
        "trigger_mode",
        "timeframe",
        "frequency",
        "cooldown_s",
        "level",
        "expires_at",
        "notify",
    ):
        value = getattr(body, name)
        if value is not None:
            setattr(alert, name, list(value) if name == "notify" else value)
    if body.name is not None:
        alert.name = body.name.strip()
