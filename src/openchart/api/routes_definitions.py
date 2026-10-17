"""The editor's endpoints for your indicator files (CI-4; docs/api.md, "Your indicator
definitions"): templates, New, Import, source and Export, Save with its history, Rename,
Duplicate, versions and Restore, Preview. Usage and Delete are in
`routes_custom_indicators.py`.

Every route is behind CI-1's editing gate (`require_editing`: the setting `on` and the
minimum isolation; 403 with the reason otherwise). Files are handled by
`indicators/_files.py` (plain files only, atomic writes, ids that are ids). Writes to one
id are serialized; each write is broadcast as `indicators_changed {id, op}`.

Preview runs unsaved source in the worker through the runner directly, never through
`CustomIndicators`: it stores nothing and counts toward no quarantine.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from openchart.api.chart_indicators import ChartIndicators
from openchart.api.editing import require_editing
from openchart.api.routes_indicators import COLUMNS, MAX_WARMUP, read_bars, shape
from openchart.indicators import templates
from openchart.indicators._base import Info, ResultError
from openchart.indicators._custom import indicator_from_json
from openchart.indicators._files import (
    FileProblem,
    Files,
    clean_source,
    id_from_filename,
    title_of,
    valid_id,
    valid_title,
    version_of,
    with_title,
)
from openchart.indicators.worker.runner import ScriptFailed, WorkerUnavailable
from openchart.market.aggregate import PRICES
from openchart.market.timeframes import TIMEFRAMES, parse_timeframe
from openchart.server import Server

TIME = {"ge": -(2**53), "le": 2**53}


class NewBody(BaseModel):
    id: Any = None
    template: Any = None
    title: Any = None
    source: Any = None
    filename: Any = None


class SaveBody(BaseModel):
    source: Any = None
    base_version: str | None = Field(None, max_length=64)


class RenameBody(BaseModel):
    title: Any = None


class DuplicateBody(BaseModel):
    id: Any = None
    title: Any = None


class RestoreBody(BaseModel):
    base_version: str | None = Field(None, max_length=64)


class PreviewBody(BaseModel):
    source: Any = None
    id: Any = None
    instrument: str = Field(max_length=40)
    timeframe: str = "1m"
    price: str = "mid"
    params: dict[str, Any] = Field(default_factory=dict)
    from_: int | None = Field(None, alias="from", **TIME)
    to: int | None = Field(None, **TIME)
    limit: int | None = Field(None, ge=1, le=2**31)


def _http(exc: FileProblem) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _script_detail(exc: ScriptFailed) -> dict[str, Any]:
    info = exc.info
    kind = "timeout" if exc.kind == "timeout" else (info.get("type") or exc.kind)
    return {
        "type": kind,
        "message": info.get("message") or exc.summary,
        "line": info.get("line"),
        "traceback": info.get("traceback") or "",
    }


def definition_routes(server: Server, charts: ChartIndicators) -> APIRouter:
    router = APIRouter(prefix="/api/custom-indicators")

    def files() -> Files:
        return Files(server.custom.folder)

    def lock(indicator_id: str) -> asyncio.Lock:
        return server.custom.file_lock(indicator_id)  # shared with the delete

    async def run(fn, *args, **kwargs):
        """A file operation, in a thread; its FileProblem as the HTTP answer."""
        try:
            return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
        except FileProblem as exc:
            raise _http(exc) from None

    def checked(fn, *args):
        try:
            return fn(*args)
        except FileProblem as exc:
            raise _http(exc) from None

    def body_id(value: Any) -> str:
        if not valid_id(value):
            raise HTTPException(
                400, f"id must be lowercase letters, digits and _ (at most 40): {value!r:.60}"
            )
        return value

    def announce(indicator_id: str, op: str) -> None:
        server.hub.broadcast({"type": "indicators_changed", "id": indicator_id, "op": op})

    async def answer(indicator_id: str, text: str, *, unchanged: bool) -> dict[str, Any]:
        """The definition as the catalog lists it, read again from the file just written."""
        server.custom.forget(indicator_id)
        d = await server.custom.get(indicator_id)
        if d is None:  # removed meanwhile by hand
            raise HTTPException(404, f"no indicator of yours named {indicator_id!r}")
        return {
            **d.to_json(),
            "version": version_of(text),
            "saved": True,
            "unchanged": unchanged,
            "error": None if d.status == "ok" else d.error,
        }

    @router.get("/templates")
    async def list_templates(request: Request) -> list[dict[str, str]]:
        await require_editing(request, server.settings, server.isolation)
        return templates.listing()

    @router.post("", status_code=201)
    async def create(body: NewBody, request: Request) -> dict[str, Any]:
        """New from a template, or Import of a source (never over an existing file)."""
        await require_editing(request, server.settings, server.isolation)
        if body.template is not None:
            indicator_id = body_id(body.id)
            text = templates.source(body.template) if isinstance(body.template, str) else None
            if text is None:
                raise HTTPException(400, f"no template {body.template!r:.60}")
            if body.title is not None:
                text = checked(with_title, text, checked(valid_title, body.title))
        elif body.source is not None:
            text = checked(clean_source, body.source)
            filename = body.filename if isinstance(body.filename, str) else None
            indicator_id = body_id(body.id if body.id is not None else id_from_filename(filename))
        else:
            raise HTTPException(400, "give a template (New) or a source (Import)")
        async with lock(indicator_id):
            await run(files().create, indicator_id, text)
        announce(indicator_id, "create")
        return await answer(indicator_id, text, unchanged=False)

    @router.get("/{indicator_id}/source")
    async def get_source(indicator_id: str, request: Request, download: bool = False) -> Any:
        await require_editing(request, server.settings, server.isolation)
        text = await run(files().read, indicator_id)
        data = text.encode("utf-8")
        if download:
            return Response(
                data,
                media_type="text/x-python; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{indicator_id}.py"'},
            )
        st = await asyncio.to_thread(os.stat, files().path(indicator_id))
        return {
            "id": indicator_id,
            "file": f"{indicator_id}.py",
            "version": version_of(text),
            "source": text,
            "bytes": len(data),
            "updated_at": int(st.st_mtime),
        }

    async def save(indicator_id: str, text: str, base: str | None, op: str) -> dict[str, Any]:
        async with lock(indicator_id):
            changed = await run(files().save, indicator_id, text, base_version=base)
        if changed:
            announce(indicator_id, op)
        return await answer(indicator_id, text, unchanged=not changed)

    @router.put("/{indicator_id}/source")
    async def put_source(indicator_id: str, body: SaveBody, request: Request) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        checked(files().path, indicator_id)
        text = checked(clean_source, body.source)
        return await save(indicator_id, text, body.base_version, "save")

    @router.patch("/{indicator_id}")
    async def rename(indicator_id: str, body: RenameBody, request: Request) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        checked(files().path, indicator_id)
        title = checked(valid_title, body.title)
        current = await run(files().read, indicator_id)
        text = checked(with_title, current, title)
        return await save(indicator_id, text, version_of(current), "rename")

    @router.post("/{indicator_id}/duplicate", status_code=201)
    async def duplicate(indicator_id: str, body: DuplicateBody, request: Request) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        source = await run(files().read, indicator_id)
        new_id = body_id(body.id) if body.id is not None else checked(files().free_id, indicator_id)
        title = checked(valid_title, body.title) if body.title is not None else None
        old = title_of(source)
        text = source
        if title is not None or old is not None:
            wanted = title if title is not None else f"{(old or '')[:53]} (copy)"
            try:
                text = with_title(source, wanted)
            except FileProblem:  # no TITLE line it can edit: the copy keeps its code as is
                text = source
        async with lock(new_id):
            await run(files().create, new_id, text)
        announce(new_id, "duplicate")
        return await answer(new_id, text, unchanged=False)

    @router.get("/{indicator_id}/versions")
    async def versions(indicator_id: str, request: Request) -> list[dict[str, Any]]:
        await require_editing(request, server.settings, server.isolation)

        def listing() -> list[dict[str, Any]]:
            out = []
            for version, path in files().versions(indicator_id):
                data = path.read_bytes()
                out.append(
                    {
                        "version": version,
                        "saved_at": _saved_at(version),
                        "bytes": len(data),
                        "digest": hashlib.sha256(data).hexdigest()[:12],
                    }
                )
            return out

        return await run(listing)

    @router.get("/{indicator_id}/versions/{version}")
    async def get_version(indicator_id: str, version: str, request: Request) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        path = await run(files().version, indicator_id, version)
        text = await run(path.read_text, encoding="utf-8")
        return {
            "id": indicator_id,
            "version": version,
            "source": text,
            "saved_at": _saved_at(version),
        }

    @router.post("/{indicator_id}/versions/{version}/restore")
    async def restore(
        indicator_id: str, version: str, request: Request, body: RestoreBody | None = None
    ) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        path = await run(files().version, indicator_id, version)
        text = checked(clean_source, await run(path.read_text, encoding="utf-8"))
        return await save(indicator_id, text, body.base_version if body else None, "restore")

    @router.post("/preview")
    async def preview(body: PreviewBody, request: Request) -> dict[str, Any]:
        """Run unsaved source on a chart query: values or the error, nothing stored."""
        await require_editing(request, server.settings, server.isolation)
        text = checked(clean_source, body.source)
        name = body_id(body.id) if body.id is not None else "preview"
        file = f"{name}.py"
        runner = server.custom.runner
        if runner is None:
            isolation = server.isolation
            why = isolation.reason if isolation else "the indicator worker is still starting"
            raise HTTPException(503, f"custom indicators do not run here: {why}")
        try:
            tf = parse_timeframe(body.timeframe)
        except ValueError:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}") from None
        if body.price not in PRICES:
            raise HTTPException(400, f"price must be one of {list(PRICES)}")
        row = await server.store.instrument(server.source, body.instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {body.instrument!r}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            schema = await runner.describe(digest, file, text)
            indicator = indicator_from_json(name, schema, file)
        except ScriptFailed as exc:
            raise HTTPException(422, _script_detail(exc)) from None
        except WorkerUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except (KeyError, TypeError, ValueError) as exc:
            detail = {"type": "SchemaError", "message": str(exc), "line": None, "traceback": ""}
            raise HTTPException(422, detail) from None
        try:
            settings = indicator.normalize(body.params)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        info = Info(
            instrument=body.instrument,
            display_name=row.display_name,
            decimals=row.decimals,
            timeframe=tf.name,
            price=body.price,
        )
        try:
            warm = min(max(await runner.warmup(digest, file, text, settings), 0), MAX_WARMUP)
            bars, frame, _ = await read_bars(
                server, body.instrument, tf, body.price, body.from_, body.to, body.limit, warm
            )
            arrays = {c: frame[c].to_numpy(dtype="float64") for c in COLUMNS}
            out = await runner.compute(digest, file, text, settings, arrays, info.to_json())
            series = indicator.check_wire(out, len(frame))
        except ScriptFailed as exc:
            raise HTTPException(422, _script_detail(exc)) from None
        except ResultError as exc:
            detail = {"type": "ResultError", "message": str(exc), "line": None, "traceback": ""}
            raise HTTPException(422, detail) from None
        except WorkerUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return {
            "id": name,
            "kind": "custom",
            "name": indicator.name,
            "instrument": body.instrument,
            "timeframe": tf.name,
            "price": body.price,
            **shape(indicator, settings, bars, series),
            "definition": {**indicator.to_json(), "kind": "custom"},
            "preview": True,
        }

    return router


def _saved_at(version: str) -> int:
    stamp = version.split("-", 1)[0]
    return int(datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp())
