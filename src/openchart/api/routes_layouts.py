"""Layout routes (SPEC sections 7 and 11): named chart layouts and the default one.

A layout holds the instrument, timeframe, chart type, price type, theme, panel state and
the indicators with their parameters. Every change is broadcast as `layouts_changed` with
the list, so open pages keep their layout menu current.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from openchart.market.timeframes import parse_timeframe
from openchart.server import Server
from openchart.store.layouts import DEFAULT_KEY, LayoutStore

ChartType = Literal["candles", "bars", "line", "area", "heikin_ashi"]
MAX_INDICATORS = 30


class LayoutIndicator(BaseModel):
    id: str = Field(max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)
    visible: bool = True
    price_labels: bool = True  # last value on the price scale
    legend_values: bool = True  # values in the legend row


class LayoutState(BaseModel):
    instrument: str | None = Field(None, max_length=40)
    timeframe: str = "15m"
    chart_type: ChartType = "candles"
    price: Literal["mid", "bid", "ask"] = "mid"
    theme: Literal["dark", "light"] = "dark"
    panel_open: bool = True
    panel_view: Literal["watchlist", "alerts"] = "watchlist"
    indicators: list[LayoutIndicator] = Field(default_factory=list, max_length=MAX_INDICATORS)

    @field_validator("timeframe")
    @classmethod
    def known_timeframe(cls, v: str) -> str:
        try:
            return parse_timeframe(v).name
        except (KeyError, ValueError):
            raise ValueError(f"unknown timeframe {v!r}") from None


class LayoutCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    layout: LayoutState
    default: bool = False


class LayoutUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    layout: LayoutState | None = None
    default: bool | None = None


def layout_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api/layouts")

    def store() -> LayoutStore:
        return LayoutStore(server.db.conn)

    async def default_id() -> str | None:
        return await server.settings.get(DEFAULT_KEY)

    def checked(state: LayoutState) -> dict[str, Any]:
        """The layout as stored: indicator parameters validated and completed."""
        data = state.model_dump()
        for entry in data["indicators"]:
            indicator = server.indicators.get(entry["id"])
            if indicator is None:
                raise HTTPException(400, f"no indicator {entry['id']!r}")
            try:
                entry["params"] = indicator.normalize(entry["params"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        return data

    async def summaries() -> list[dict[str, Any]]:
        current = await default_id()
        return [layout.summary(current) for layout in await store().list()]

    async def announce() -> None:
        server.hub.broadcast({"type": "layouts_changed", "layouts": await summaries()})

    @router.get("")
    async def list_layouts() -> list[dict[str, Any]]:
        return await summaries()

    @router.get("/{layout_id}")
    async def get(layout_id: str) -> dict[str, Any]:
        layout = await store().get(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        return layout.to_json(await default_id())

    @router.post("")
    async def create(body: LayoutCreate) -> dict[str, Any]:
        layout = await store().create(body.name.strip(), checked(body.layout))
        if body.default:
            await server.settings.set(DEFAULT_KEY, layout.id)
        await announce()
        return layout.to_json(await default_id())

    @router.put("/{layout_id}")
    async def update(layout_id: str, body: LayoutUpdate) -> dict[str, Any]:
        layout = await store().get(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        if body.name is not None:
            layout.name = body.name.strip()
        if body.layout is not None:
            layout.state = checked(body.layout)
        if body.name is not None or body.layout is not None:
            await store().save(layout)
        if body.default is True:
            await server.settings.set(DEFAULT_KEY, layout.id)
        elif body.default is False and await default_id() == layout.id:
            await server.settings.delete(DEFAULT_KEY)
        await announce()
        return layout.to_json(await default_id())

    @router.delete("/{layout_id}")
    async def delete(layout_id: str) -> dict[str, Any]:
        current = await default_id()
        layout = await store().delete(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        if current == layout_id:
            await server.settings.delete(DEFAULT_KEY)
        await announce()
        return layout.summary(None)

    return router
