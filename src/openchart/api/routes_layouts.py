"""Layout routes (SPEC sections 7 and 11): named chart layouts and the default one.

A layout holds the instrument, timeframe, chart type, price type, theme, panel state and
a copy of the chart's indicators with their parameters (CI-3): a layout saved without
`indicators` copies its instrument's list from the server, and `apply` puts the copy
back on a chart. Every change is broadcast as `layouts_changed` with the list, so open
pages keep their layout menu current.

Applying keeps the chart's instances where the layout has them (docs/decisions-indicator-
alert-gaps.md): each layout entry takes the uid of the chart instance it matches, first
the one with its uid and definition, then the first unmatched one, in legend order, with
the same definition and value inputs; so indicator alerts on them stay armed (or re-arm
when the values differ). `apply-preview` lists the alerts an apply would stop or re-arm;
an apply that would stop alerts is refused (409, with the preview) unless each of them is
named (`stop_alerts=<id>`).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from openchart.api.chart_indicators import (
    ChartIndicators,
    ChartWrite,
    InstanceList,
    new_uid,
    parse_entries,
)
from openchart.market.timeframes import parse_timeframe
from openchart.server import Server
from openchart.store.chart_indicators import Instance
from openchart.store.layouts import DEFAULT_KEY, Layout, LayoutStore

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

    async def values(i: Instance) -> dict[str, Any]:
        """An instance's value inputs (colours and widths left out): what matching compares."""
        _, schema, _ = await charts.definition(i.kind, i.indicator)
        if schema is None:
            return dict(i.params)
        style = {p.name for p in schema.params if p.group == "style"}
        return {k: v for k, v in i.params.items() if k not in style}

    async def matched(
        instances: list[Instance], resets: dict[str, list[str]], current: list[Instance]
    ) -> tuple[list[Instance], dict[str, list[str]]]:
        """The layout's instances with the uids of the chart instances they match: first
        the same uid and definition, then the first unmatched instance with the same
        definition and value inputs, in legend order. An unmatched entry never takes a
        uid that is on the chart (an alert would follow it to another indicator)."""
        taken: set[str] = set()
        uid_of: dict[int, str] = {}
        by_uid = {c.uid: c for c in current}
        for n, i in enumerate(instances):
            c = by_uid.get(i.uid)
            if c is not None and (c.kind, c.indicator) == (i.kind, i.indicator):
                uid_of[n] = c.uid
                taken.add(c.uid)
        for n, i in enumerate(instances):
            if n in uid_of:
                continue
            mine = await values(i)
            for c in current:
                same = (c.kind, c.indicator) == (i.kind, i.indicator)
                if c.uid not in taken and same and await values(c) == mine:
                    uid_of[n] = c.uid
                    taken.add(c.uid)
                    break
        out: list[Instance] = []
        new_resets: dict[str, list[str]] = {}
        used = set(uid_of.values())
        for n, i in enumerate(instances):
            uid = uid_of.get(n)
            if uid is None:
                uid = i.uid if i.uid not in by_uid and i.uid not in used else new_uid()
                used.add(uid)
            if i.uid in resets:
                new_resets[uid] = resets[i.uid]
            out.append(
                Instance(
                    uid, i.kind, i.indicator, i.params, i.visible, i.price_labels, i.legend_values
                )
            )
        return out, new_resets

    def removed(layout: Layout, instrument: str) -> dict[str, str]:
        return {
            "removed_reason": f'the layout "{layout.name}" applied to the chart does not have'
            " its indicator",
            "removed_text": f'Applying the layout "{layout.name}" removed the indicator from'
            f" the {instrument} chart",
        }

    async def planned(layout_id: str, instrument: str | None) -> tuple[Any, ...]:
        """(layout, instrument, the chart write, entries left out, what it does to the
        indicator alerts). Holding `charts.lock`."""
        layout = await store().get(layout_id)
        if layout is None:
            raise HTTPException(404, "no such layout")
        instrument = instrument or layout.state.get("instrument")
        if not instrument:
            raise HTTPException(400, "the layout has no instrument: give ?instrument=")
        await charts.known_chart(server.source, instrument)
        entries, skipped = parse_entries(layout.state.get("indicators") or [])
        instances, resets, left_out = await charts.checked(entries, strict=False)
        current = await charts.store().list(server.source, instrument)
        instances, resets = await matched(instances, resets, current)
        write = ChartWrite(server.source, instrument, instances, resets)
        changes = await charts.plan([write], **removed(layout, instrument))
        return layout, instrument, write, skipped + left_out, changes

    def preview_of(instrument: str, changes: list[Any]) -> dict[str, Any]:
        return {
            "instrument": instrument,
            "alerts_stopped": [c.preview() for c in changes if c.stop is not None],
            "alerts_rearmed": [c.preview() for c in changes if c.rearm is not None],
        }

    @router.get("/{layout_id}/apply-preview")
    async def apply_preview(layout_id: str, instrument: str | None = None) -> dict[str, Any]:
        """What applying the layout would do to the chart's indicator alerts. Changes
        nothing."""
        async with charts.lock:
            _, instrument, _, _, changes = await planned(layout_id, instrument)
        return preview_of(instrument, changes)

    @router.post("/{layout_id}/apply")
    async def apply(
        layout_id: str,
        instrument: str | None = None,
        stop_alerts: list[str] = Query(default=[]),  # noqa: B008  (FastAPI's way)
    ) -> dict[str, Any]:
        """Put the layout's copy of the indicators on a chart (the layout's instrument
        unless another is given). Values that no longer fit fall back with a note;
        entries whose definition is gone are left out and listed in `skipped`. Refused
        (409, with the preview) unless `stop_alerts` names exactly the indicator alerts
        it would stop."""
        async with charts.lock:
            layout, instrument, write, skipped, changes = await planned(layout_id, instrument)
            preview = preview_of(instrument, changes)
            stopping = {x["id"] for x in preview["alerts_stopped"]}
            if set(stop_alerts) != stopping:
                many = len(stopping) != 1
                message = (
                    f"Applying it stops {len(stopping)} indicator alert{'s' if many else ''}:"
                    " name each of them to confirm (stop_alerts=<id>)."
                    if stopping
                    else "Applying it stops no indicator alert: name none."
                )
                raise HTTPException(409, {"message": message, "preview": preview})
            (chart,) = await charts.commit([write], **removed(layout, instrument))
        return {
            "layout": layout.to_json(await default_id()),
            "chart": chart,
            "skipped": skipped,
            "alerts_stopped": preview["alerts_stopped"],
            "alerts_rearmed": preview["alerts_rearmed"],
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
