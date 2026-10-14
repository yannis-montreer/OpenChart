"""Alert routes (SPEC sections 7 and 9). Changes are broadcast as `alert_changed`."""

from __future__ import annotations

import math
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from openchart.alerts.notify import default_name
from openchart.market.timeframes import Kind, parse_timeframe
from openchart.server import Server
from openchart.store.alerts import Alert
from openchart.store.drawings import DrawingStore

AlertType = Literal["line_cross", "level_cross"]
Direction = Literal["cross", "up", "down"]
PriceKind = Literal["bid", "ask", "mid"]
Mode = Literal["touch", "candle_close"]
Frequency = Literal["once", "once_per_bar", "repeating"]
Channel = Literal["telegram", "app"]


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
        instrument: str | None = None, drawing_id: str | None = None
    ) -> list[dict[str, Any]]:
        alerts = await service().store.list(instrument)
        if drawing_id is not None:
            alerts = [a for a in alerts if a.drawing_id == drawing_id]
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
        alert = Alert(id="", instrument=body.instrument, type=body.type)
        if body.type == "line_cross":
            drawing = await DrawingStore(server.db.conn).get(body.drawing_id or "")
            if drawing is None or drawing.instrument != body.instrument:
                raise HTTPException(400, "a line alert needs a line on this instrument")
            alert.drawing_id = drawing.id
        elif body.level is None:
            raise HTTPException(400, "a level alert needs a price level")
        _apply(alert, body)
        if alert.expires_at is not None and alert.expires_at <= time.time():
            raise HTTPException(400, "the expiration is in the past")
        if not alert.name:
            alert.name = default_name(alert, await line_type(alert), row.decimals)
        alert = await service().store.create(alert)
        service().put(alert)
        return alert.to_json()

    @router.put("/{alert_id}")
    async def update(alert_id: str, body: AlertUpdate) -> dict[str, Any]:
        alert = service().engine.alerts.get(alert_id) or await service().store.get(alert_id)
        if alert is None:
            raise HTTPException(404, "no such alert")
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

    @router.delete("/{alert_id}")
    async def delete(alert_id: str) -> dict[str, Any]:
        alert = await service().store.delete(alert_id)
        if alert is None:
            raise HTTPException(404, "no such alert")
        service().removed(alert)
        return alert.to_json()

    return router


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
