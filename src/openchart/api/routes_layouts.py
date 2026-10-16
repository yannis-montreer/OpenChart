"""Layout routes (SPEC sections 7 and 11): named chart layouts and the default one.

A layout holds the instrument, timeframe, chart type, price type, theme, panel state and
a copy of the chart's indicators with their parameters (CI-3): a layout saved without
`indicators` copies its instrument's list from the server, and `apply` puts the copy
back on a chart. Every change is broadcast as `layouts_changed` with the list, so open
pages keep their layout menu current.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from openchart.api.chart_indicators import ChartIndicators, InstanceList, parse_entries
from openchart.market.timeframes import parse_timeframe
from openchart.server import Server
from openchart.store.layouts import DEFAULT_KEY, LayoutStore

ChartType = Literal["candles", "bars", "line", "area", "heikin_ashi"]


class LayoutState(BaseModel):
    instrument: str | None = Field(None, max_length=40)
    timeframe: str = "15m"
    chart_type: ChartType = "candles"
    price: Literal["mid", "bid", "ask"] = "mid"
    theme: Literal["dark", "light"] = "dark"
    panel_open: bool = True
    panel_view: Literal["watchlist", "alerts"] = "watchlist"
    # None: a copy of the instrument's indicators on the server (CI-3); a list: these.
    indicators: InstanceList | None = None

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


async def layouts_message(server: Server) -> dict[str, Any]:
    """The `layouts_changed` broadcast: the list, as `GET /api/layouts` answers it."""
    current = await server.settings.get(DEFAULT_KEY)
    layouts = await LayoutStore(server.db.conn).list()
    return {"type": "layouts_changed", "layouts": [x.summary(current) for x in layouts]}


def layout_routes(server: Server, charts: ChartIndicators) -> APIRouter:
    router = APIRouter(prefix="/api/layouts")

    def store() -> LayoutStore:
        return LayoutStore(server.db.conn)

    async def default_id() -> str | None:
        return await server.settings.get(DEFAULT_KEY)

    async def checked(state: LayoutState) -> dict[str, Any]:
        """The layout as stored: indicators checked and completed like a chart's (a kind
        and a uid each), or copied from the instrument's chart. Call it holding
        `charts.lock` and store the result before releasing it, so a definition deleted
        meanwhile cannot end up in the layout."""
        assert charts.lock.locked()
        data = state.model_dump()
        if state.indicators is not None:
            instances, _, _ = await charts.checked(state.indicators, strict=True)
        elif state.instrument is not None:
            instances = await charts.store().list(server.source, state.instrument)
        else:
            instances = []
        data["indicators"] = [i.to_json() for i in instances]
        return data

    async def announce() -> None:
        server.hub.broadcast(await layouts_message(server))

    @router.get("")
    async def list_layouts() -> list[dict[str, Any]]:
        return (await layouts_message(server))["layouts"]

    @router.get("/{layout_id}")
    async def get(layout_id: str) -> dict[str, Any]:
        layout = await store().get(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        return layout.to_json(await default_id())

    @router.post("")
    async def create(body: LayoutCreate) -> dict[str, Any]:
        async with charts.lock:
            layout = await store().create(body.name.strip(), await checked(body.layout))
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
        async with charts.lock:
            if body.layout is not None:
                layout.state = await checked(body.layout)
            if body.name is not None or body.layout is not None:
                await store().save(layout)
        if body.default is True:
            await server.settings.set(DEFAULT_KEY, layout.id)
        elif body.default is False and await default_id() == layout.id:
            await server.settings.delete(DEFAULT_KEY)
        await announce()
        return layout.to_json(await default_id())

    @router.post("/{layout_id}/apply")
    async def apply(layout_id: str, instrument: str | None = None) -> dict[str, Any]:
        """Put the layout's copy of the indicators on a chart (the layout's instrument
        unless another is given). Values that no longer fit fall back with a note;
        entries whose definition is gone are left out and listed in `skipped`."""
        layout = await store().get(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        instrument = instrument or layout.state.get("instrument")
        if not instrument:
            raise HTTPException(400, "the layout has no instrument: give ?instrument=")
        await charts.known_chart(server.source, instrument)
        entries, skipped = parse_entries(layout.state.get("indicators") or [])
        async with charts.lock:
            instances, resets, left_out = await charts.checked(entries, strict=False)
            chart = await charts.replace(server.source, instrument, instances, resets)
        return {
            "layout": layout.to_json(await default_id()),
            "chart": chart,
            "skipped": skipped + left_out,
        }

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
