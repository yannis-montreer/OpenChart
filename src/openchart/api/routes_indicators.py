"""Indicator routes (SPEC sections 7 and 11): the catalog, and values aligned to candles.

Values are computed on the same bars `/api/candles` returns for the same query
(instrument, timeframe, price type, range), plus as many earlier bars as the indicator
asks for to settle (`warmup`). Numbers: one value per bar, null where there is none.
Markers and backgrounds: the list of bar positions (into `t`) where they are on. Levels:
their value for these parameters. See docs/api.md.

Built-in indicators are computed here, in the server; your own files in the worker
(`indicators/worker/`). The two are separate namespaces: `kind=builtin|custom` picks one;
without it, a built-in wins and a custom file is found when no built-in has its id.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request

from openchart.api.editing import editing_for
from openchart.indicators._base import Info
from openchart.indicators.worker.runner import ScriptFailed, WorkerUnavailable
from openchart.market.aggregate import PRICES
from openchart.market.series import MAX_LIMIT
from openchart.market.timeframes import TIMEFRAMES, parse_timeframe
from openchart.server import Server

MAX_WARMUP = 20_000  # bars
COLUMNS = ["ts", "o", "h", "l", "c", "v"]


def indicator_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api/indicators")

    @router.get("")
    async def catalog(request: Request) -> dict[str, Any]:
        registry = server.indicators
        custom = await server.custom.refresh()
        isolation = server.isolation
        builtin = [{**i.to_json(), "kind": "builtin"} for i in registry.catalog()]
        runnable = [
            d.to_json()["definition"]
            for d in custom
            if d.indicator is not None and d.status == "ok" and d.id not in registry.indicators
        ]
        return {
            # The flat list the M7 menu reads: built-in ones, then yours that run (a file
            # with a built-in's id is only in "custom" until the menu has sections, CI-3).
            "indicators": sorted(builtin + runnable, key=lambda i: i["title"].lower()),
            "errors": registry.errors
            + [
                {"file": d.file, "error": d.reason or d.status}
                for d in custom
                if d.status in ("error", "quarantined", "not_run")
            ],
            "folder": str(server.indicators_dir),
            "builtin": builtin,
            "custom": [d.to_json() for d in custom],
            "isolation": isolation.to_json() if isolation else {"level": "starting"},
            "worker": server.indicator_runner.status() if server.indicator_runner else None,
            "editing": (await editing_for(request, server.settings, isolation)).to_json(),
        }

    @router.get("/{indicator_id}")
    async def values(
        indicator_id: str,
        instrument: str,
        timeframe: str = "1m",
        price: str = "mid",
        params: str = "{}",
        kind: str | None = None,
        from_: int | None = Query(None, alias="from"),
        to: int | None = None,
        limit: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        if kind not in (None, "builtin", "custom"):
            raise HTTPException(400, "kind must be builtin or custom")
        builtin = server.indicators.get(indicator_id) if kind != "custom" else None
        custom = None
        if builtin is None and kind != "builtin":
            custom = await server.custom.get(indicator_id)
        if builtin is None and custom is None:
            raise HTTPException(404, f"no indicator {indicator_id!r}")
        if custom is not None and custom.indicator is None:
            status = 409 if custom.status == "quarantined" else 422
            if custom.status == "not_run":
                status = 503
            raise HTTPException(status, f"{custom.file}: {custom.reason or custom.status}")
        indicator = builtin or custom.indicator
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
        row = await server.store.instrument(server.source, instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {instrument!r}")
        info = Info(
            instrument=instrument,
            display_name=row.display_name,
            decimals=row.decimals,
            timeframe=tf.name,
            price=price,
        )

        try:
            if custom is not None:
                warm = await server.custom.warmup(custom, settings)
            else:
                warm = int(indicator.warmup(settings))
        except ScriptFailed as exc:
            raise _failed(indicator.name, exc) from None
        except WorkerUnavailable as exc:
            raise HTTPException(503, f"{indicator.name}: {exc}") from None
        except Exception as exc:  # a built-in's own code
            raise HTTPException(
                422, f"{indicator.name} failed: {type(exc).__name__}: {exc}"
            ) from None
        warm = min(max(warm, 0), MAX_WARMUP)

        read = server.reader.candles
        bars = await read(
            server.source, instrument, tf, price=price, start=from_, end=to, limit=limit
        )
        bars = bars[COLUMNS]
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
        frame = frame.reset_index(drop=True)

        try:
            if custom is not None:
                series = await server.custom.compute(custom, frame, settings, info, warmup=warm)
            else:
                series = await asyncio.to_thread(indicator.run, frame, settings, info)
        except ScriptFailed as exc:
            raise _failed(indicator.name, exc) from None
        except WorkerUnavailable as exc:
            raise HTTPException(503, f"{indicator.name}: {exc}") from None
        except Exception as exc:  # a built-in's own code
            raise HTTPException(
                422, f"{indicator.name} failed: {type(exc).__name__}: {exc}"
            ) from None
        n = len(bars)
        numbers: dict[str, list[float | None]] = {}
        hits: dict[str, list[int]] = {}
        levels: dict[str, float] = {}
        for oid, o in zip(indicator.output_ids(), indicator.outputs, strict=True):
            if o.data == "number":
                numbers[oid] = _floats(series[oid].iloc[len(series[oid]) - n :])
            elif o.data == "bool":
                tail = series[oid].to_numpy()[len(series[oid]) - n :]
                hits[oid] = np.flatnonzero(tail).tolist()
            elif o.kind == "level":
                levels[oid] = float(settings[o.value] if isinstance(o.value, str) else o.value)
        out = {
            "id": indicator.id,
            "kind": "custom" if custom is not None else "builtin",
            "name": indicator.name,
            "instrument": instrument,
            "timeframe": tf.name,
            "price": price,
            "params": settings,
            "t": [int(t) for t in bars["ts"]],
            "values": numbers,
            "hits": hits,
            "levels": levels,
        }
        if custom is not None:  # the save-time checks of this version with these settings
            out["checks"] = server.custom.checks_for(custom, settings)
        return out

    return router


def _failed(name: str, exc: ScriptFailed) -> HTTPException:
    """The legend's "⚠ SMA cross: ZeroDivisionError (line 23)"; 409 once quarantined."""
    status = 409 if exc.kind == "quarantined" else 422
    return HTTPException(status, f"{name} failed: {exc.summary}")


def _floats(series: pd.Series) -> list[float | None]:
    return [None if not math.isfinite(v) else float(v) for v in series.tolist()]
