"""A chart's indicators (CI-3, decision D1): the same in every browser and on the phone.

Every change is broadcast as `chart_indicators_changed` with the chart's list, as `GET`
answers it. These routes are ordinary chart settings: they need the login and the
same-origin check, not the custom-indicator editing gate (adding an indicator from the
phone is not editing code). See `chart_indicators.py` and docs/api.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openchart.api.chart_indicators import ChartIndicators, InstanceList
from openchart.server import Server


class ChartPut(BaseModel):
    indicators: InstanceList
    revision: int | None = None  # the revision this list was based on; 409 if it moved on


class ChartImport(BaseModel):
    indicators: InstanceList


def chart_routes(server: Server, charts: ChartIndicators) -> APIRouter:
    router = APIRouter(prefix="/api/charts/{source}/{instrument}/indicators")

    @router.get("")
    async def get(source: str, instrument: str) -> dict[str, Any]:
        await charts.known_chart(source, instrument)
        return await charts.view(source, instrument)

    @router.put("")
    async def put(source: str, instrument: str, body: ChartPut) -> dict[str, Any]:
        await charts.known_chart(source, instrument)
        async with charts.lock:
            current = await charts.store().revision(source, instrument)
            if body.revision is not None and body.revision != current:
                raise HTTPException(
                    409,
                    {
                        "message": "the chart's indicators were changed elsewhere",
                        "chart": await charts.view(source, instrument),
                    },
                )
            instances, resets, _ = await charts.checked(body.indicators, strict=True)
            return await charts.replace(source, instrument, instances, resets)

    @router.post("/import")
    async def import_(source: str, instrument: str, body: ChartImport) -> dict[str, Any]:
        """A browser's list from before CI-3 (its `localStorage`), taken only onto a chart
        whose list was never set, also not to an empty one."""
        await charts.known_chart(source, instrument)
        async with charts.lock:
            if await charts.store().revision(source, instrument) != 0:
                raise HTTPException(
                    409,
                    {
                        "message": "this chart's indicators are already on the server",
                        "chart": await charts.view(source, instrument),
                    },
                )
            instances, resets, skipped = await charts.checked(body.indicators, strict=False)
            chart = await charts.replace(source, instrument, instances, resets)
        return {**chart, "skipped": skipped}

    return router
