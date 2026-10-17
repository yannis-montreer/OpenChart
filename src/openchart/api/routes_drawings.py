"""Drawing routes (SPEC section 7). Every change is broadcast as `drawing_changed`."""

from __future__ import annotations

import math
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from openchart.drawing_rules import shape_error
from openchart.market.sessions import trading_clock
from openchart.server import Server
from openchart.store.drawings import Drawing, DrawingStore

DrawingType = Literal[
    "hline", "trendline", "ray", "extended", "vline", "text", "rect", "fib", "measure", "channel"
]


class Point(BaseModel):
    t: int = Field(ge=0, le=2**33)
    p: float

    @field_validator("p")
    @classmethod
    def finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("price must be a finite number")
        return v


class Style(BaseModel):
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    width: int | None = Field(None, ge=1, le=4)
    dash: Literal["solid", "dashed", "dotted"] | None = None
    fill: StrictInt | None = Field(None, ge=0, le=100)  # opacity %, rect and channel
    extend: StrictBool | None = None  # channel: rays to the right; fib: levels to the edge
    locked: StrictBool | None = None  # the chart will not move or delete it (the API still can)
    hidden: StrictBool | None = None  # not drawn

    def values(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class DrawingCreate(BaseModel):
    instrument: str
    type: DrawingType
    points: list[Point]
    style: Style = Style()
    label: str | None = Field(None, max_length=100)


class DrawingUpdate(BaseModel):
    points: list[Point] | None = None
    style: Style | None = None
    label: str | None = Field(None, max_length=100)
    clear_label: bool = False


def check_shape(
    type_: str, points: list[Point], clock=None, label: str | None = None
) -> list[dict[str, float]]:
    pts = [{"t": pt.t, "p": pt.p} for pt in points]
    error = shape_error(type_, pts, clock, label)
    if error:
        raise HTTPException(400, error)
    return pts


def drawing_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api/drawings")

    def store() -> DrawingStore:
        return DrawingStore(server.db.conn)

    def announce(op: str, drawing: Drawing) -> dict[str, Any]:
        if op != "delete":
            server.alerts.drawing_changed(drawing)  # a moved line re-arms its alerts
        data = drawing.to_json()
        server.hub.broadcast({"type": "drawing_changed", "op": op, "drawing": data})
        return data

    @router.get("")
    async def list_drawings(instrument: str | None = None) -> list[dict[str, Any]]:
        return [d.to_json() for d in await store().list(instrument)]

    @router.post("")
    async def create(body: DrawingCreate) -> dict[str, Any]:
        row = await server.store.instrument(server.source, body.instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {body.instrument!r}")
        closures = await server.store.closures(server.source, body.instrument)
        points = check_shape(body.type, body.points, trading_clock(row.hours, closures), body.label)
        drawing = await store().create(
            body.instrument, body.type, points, body.style.values(), body.label
        )
        return announce("create", drawing)

    @router.put("/{drawing_id}")
    async def update(drawing_id: str, body: DrawingUpdate) -> dict[str, Any]:
        current = await store().get(drawing_id)
        if current is None:
            raise HTTPException(404, "no such drawing")
        points = None
        label = current.label if body.label is None else body.label
        if body.clear_label:
            label = None
        # A label-only change is checked only where the label is part of the shape: the
        # stored anchors may have lost their trading time to a closure learned since.
        text_changed = current.type == "text" and (body.clear_label or body.label is not None)
        if body.points is not None or text_changed:
            row = await server.store.instrument(server.source, current.instrument)
            closures = await server.store.closures(server.source, current.instrument)
            clock = trading_clock(row.hours, closures) if row else None
            if body.points is not None:
                points = check_shape(current.type, body.points, clock, label)
            else:
                check_shape(current.type, [Point(**p) for p in current.points], clock, label)
        drawing = await store().update(
            drawing_id,
            points=points,
            style=body.style.values() if body.style else None,
            label=body.label,
            clear_label=body.clear_label,
        )
        assert drawing is not None
        return announce("update", drawing)

    @router.delete("/{drawing_id}")
    async def delete(drawing_id: str) -> dict[str, Any]:
        drawing = await store().delete(drawing_id)
        if drawing is None:
            raise HTTPException(404, "no such drawing")
        await server.alerts.drawing_deleted(drawing.id)  # its alerts go with it
        return announce("delete", drawing)

    @router.delete("")
    async def delete_all(instrument: str) -> dict[str, Any]:
        removed = await store().delete_all(instrument)
        for drawing in removed:
            await server.alerts.drawing_deleted(drawing.id)
            announce("delete", drawing)
        return {"deleted": len(removed)}

    return router
