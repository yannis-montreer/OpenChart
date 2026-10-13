"""Indicator routes (SPEC sections 7 and 11): the catalog, and values aligned to candles.

Values are computed here, in Python, on the same bars `/api/candles` returns for the same
query (instrument, timeframe, price type, range), plus as many earlier bars as the
indicator asks for to settle (`warmup`). One value per bar; null where there is none.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from openchart.market.aggregate import PRICES
from openchart.market.series import MAX_LIMIT
from openchart.market.timeframes import TIMEFRAMES, parse_timeframe
from openchart.server import Server

MAX_WARMUP = 20_000  # bars
COLUMNS = ["ts", "o", "h", "l", "c", "v"]


def indicator_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api/indicators")

    @router.get("")
    async def catalog() -> dict[str, Any]:
        registry = server.indicators
        return {
            "indicators": [i.to_json() for i in registry.catalog()],
            "errors": registry.errors,
            "folder": str(server.indicators_dir),
        }

    @router.get("/{indicator_id}")
    async def values(
        indicator_id: str,
        instrument: str,
        timeframe: str = "1m",
        price: str = "mid",
        params: str = "{}",
        from_: int | None = Query(None, alias="from"),
        to: int | None = None,
        limit: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        indicator = server.indicators.get(indicator_id)
        if indicator is None:
            raise HTTPException(404, f"no indicator {indicator_id!r}")
        try:
            given = json.loads(params)
            if not isinstance(given, dict):
                raise ValueError("params must be a JSON object")
            settings = indicator.normalize(given)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        try:
            tf = parse_timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}") from None
        if price not in PRICES:
            raise HTTPException(400, f"price must be one of {list(PRICES)}")
        if await server.store.instrument(server.source, instrument) is None:
            raise HTTPException(404, f"unknown instrument {instrument!r}")

        read = server.reader.candles
        bars = await read(
            server.source, instrument, tf, price=price, start=from_, end=to, limit=limit
        )
        bars = bars[COLUMNS]
        warm = min(max(int(indicator.warmup(settings)), 0), MAX_WARMUP)
        before: list[pd.DataFrame] = []
        if warm and not bars.empty:
            first, need = int(bars["ts"].iloc[0]), warm
            while need > 0:  # page back, MAX_LIMIT bars at a time
                older = await read(
                    server.source,
                    instrument,
                    tf,
                    price=price,
                    end=first,
                    limit=min(need, MAX_LIMIT),
                )
                if older.empty:
                    break
                before.insert(0, older[COLUMNS])
                first, need = int(older["ts"].iloc[0]), need - len(older)
        frame = pd.concat([*before, bars], ignore_index=True) if before else bars

        try:
            series = await asyncio.to_thread(indicator.run, frame.reset_index(drop=True), settings)
        except Exception as exc:  # the indicator's own code
            raise HTTPException(
                422, f"{indicator.name} failed: {type(exc).__name__}: {exc}"
            ) from None
        n = len(bars)
        return {
            "id": indicator.id,
            "name": indicator.name,
            "instrument": instrument,
            "timeframe": tf.name,
            "price": price,
            "params": settings,
            "t": [int(t) for t in bars["ts"]],
            "values": {name: _floats(s.iloc[len(s) - n :]) for name, s in series.items()},
        }

    return router


def _floats(series: pd.Series) -> list[float | None]:
    return [None if not math.isfinite(v) else float(v) for v in series.tolist()]
