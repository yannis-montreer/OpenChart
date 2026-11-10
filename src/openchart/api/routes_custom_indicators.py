"""Your own indicator definitions: where one is used, and deleting it (CI-3; design 5.4).

Both are behind CI-1's editing gate (`api/editing.py`: the minimum isolation, then the
`indicator-editing` setting and where the request comes from), like every endpoint
under `/api/custom-indicators` (design section 4): 403 with the reason otherwise.

Deleting moves `<id>.py` and its `.history/<id>/` into `.deleted/<id>-<UTC time>/`
(`<id>.py` and `history/`), so nothing is erased. A definition in use answers 409 with
where it is used, the indicator alerts on it included; `?confirm=true` (and, when alerts
use it, every one of them named: `&alerts=<id>&alerts=<id>`) removes its instances from
every chart and saved layout and stops its active alerts, in one transaction, then moves
the files; if any step fails, nothing of it stays (docs/decisions-indicator-alert-gaps.md).
Only custom instances go: a built-in with the same id is another indicator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from openchart.api.chart_indicators import ChartIndicators, ChartWrite
from openchart.api.editing import require_editing
from openchart.api.routes_layouts import layouts_message
from openchart.files import move
from openchart.server import Server
from openchart.store.layouts import LayoutStore

ID = re.compile(r"[a-z][a-z0-9_]{0,39}")  # used with fullmatch: no trailing newline


def custom_routes(server: Server, charts: ChartIndicators) -> APIRouter:
    router = APIRouter(prefix="/api/custom-indicators")

    def definition_file(indicator_id: str) -> Path:
        """`<id>.py` in your folder (a file, or a link the folder's listing shows), or 404."""
        path = server.custom.folder / f"{indicator_id}.py"
        if not ID.fullmatch(indicator_id) or not (path.is_file() or path.is_symlink()):
            raise HTTPException(404, f"no indicator of yours named {indicator_id!r}")
        return path

    def uses(entry: Any, indicator_id: str) -> bool:
        """Is a layout's entry this custom definition? One without a kind (saved before
        CI-3) is when no built-in has the id."""
        if not isinstance(entry, dict) or entry.get("id") != indicator_id:
            return False
        kind = entry.get("kind")
        return kind == "custom" or (kind is None and server.indicators.get(indicator_id) is None)

    async def usage(indicator_id: str) -> dict[str, Any]:
        used = await charts.store().charts_using("custom", indicator_id)
        layouts = []
        for layout in await LayoutStore(server.db.conn).list():
            n = sum(uses(e, indicator_id) for e in layout.state.get("indicators") or [])
            if n:
                layouts.append({"id": layout.id, "name": layout.name, "instances": n})
        alerts = (
            [
                {
                    "id": a.id,
                    "name": a.name,
                    "instrument": a.instrument,
                    "uid": a.indicator.uid,
                    "status": a.status,
                }
                for a in await server.alerts.store.list()
                if a.type == "indicator"
                and a.indicator is not None
                and a.indicator.kind == "custom"
                and a.indicator.id == indicator_id
            ]
            if server.alerts is not None
            else []
        )
        return {
            "id": indicator_id,
            "in_use": bool(used or layouts or alerts),
            "charts": [{"source": s, "instrument": i, "instances": n} for s, i, n in used],
            "layouts": layouts,
            "alerts": alerts,
        }

    async def title(indicator_id: str) -> str:
        d = await server.custom.get(indicator_id)
        return d.indicator.title if d is not None and d.indicator is not None else indicator_id

    def where(usage: dict[str, Any], title: str) -> str:
        charts_, layouts, alerts = usage["charts"], usage["layouts"], usage["alerts"]
        parts = []
        if charts_:
            names = ", ".join(c["instrument"] for c in charts_)
            parts.append(f"on {len(charts_)} chart{'s' if len(charts_) > 1 else ''} ({names})")
        if layouts:
            names = ", ".join(f'"{x["name"]}"' for x in layouts)
            parts.append(
                f"in {len(layouts)} saved layout{'s' if len(layouts) > 1 else ''} ({names})"
            )
        text = (
            f'"{title}" is {" and ".join(parts)}. Deleting it removes it from all of them.'
            if parts
            else f'"{title}" is on no chart or layout.'
        )
        if alerts:
            many = len(alerts) != 1
            names = ", ".join(a["name"] or a["id"] for a in alerts)
            text += (
                f" {len(alerts)} indicator alert{'s' if many else ''} use{'' if many else 's'}"
                f" it ({names}): deleting it stops {'them' if many else 'it'} (settings kept)."
                " Name each of them to confirm (alerts=<id>)."
            )
        return text

    def target_folder(indicator_id: str) -> tuple[Path, Path | None]:
        """Where the files go, made now, and the history folder to move with them (None
        if there is none). 409 if `.deleted` or `.history` is not a plain folder inside
        yours (a file, or a link elsewhere): nothing is changed then."""
        folder = server.custom.folder
        root = folder.resolve()
        deleted = folder / ".deleted"
        if os.path.lexists(deleted) and (
            deleted.is_symlink() or not deleted.is_dir() or deleted.resolve() != root / ".deleted"
        ):
            raise HTTPException(409, f"{deleted} is not a folder: move it aside to delete")
        history: Path | None = folder / ".history" / indicator_id
        if not os.path.lexists(history):
            history = None
        elif history.parent.resolve() != root / ".history" or history.is_symlink():
            raise HTTPException(409, f"{history} is not a folder inside {folder}")
        deleted.mkdir(exist_ok=True)
        stamp = datetime.fromtimestamp(time.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
        for n in range(1, 1000):
            target = deleted / (f"{indicator_id}-{stamp}" + (f"-{n}" if n > 1 else ""))
            try:
                target.mkdir()
                return target, history
            except FileExistsError:
                continue
        raise HTTPException(409, f"too many deleted copies of {indicator_id} at {stamp}")

    def put_back(moved: list[tuple[Path, Path]], target: Path) -> None:
        """Undo the moves of a delete that failed, newest first, and drop its empty folder."""
        for src, dst in reversed(moved):
            move(dst, src)
        with contextlib.suppress(OSError):
            target.rmdir()

    async def remove_instances(indicator_id: str, usage: dict[str, Any], title: str) -> None:
        """Take the definition off every chart and layout using it and stop its active
        alerts, in one transaction (`ChartIndicators.commit`): if a step fails, nothing
        of it stays, and the caller moves the files back. Holding `charts.lock`."""
        writes = []
        for c in usage["charts"]:
            source, instrument = c["source"], c["instrument"]
            before = await charts.store().list(source, instrument)
            kept = [i for i in before if not (i.kind == "custom" and i.indicator == indicator_id)]
            writes.append(ChartWrite(source, instrument, kept))
        layouts = []
        for summary in usage["layouts"]:
            layout = await LayoutStore(server.db.conn).get(summary["id"])
            if layout is None:
                continue
            entries = layout.state.get("indicators") or []
            layout.state["indicators"] = [e for e in entries if not uses(e, indicator_id)]
            layouts.append(layout)
        reason = f'its indicator "{title}" was deleted'
        await charts.commit(
            writes,
            layouts=layouts,
            stops={a["id"]: reason for a in usage["alerts"] if a["status"] == "active"},
            stop_text=f'The indicator "{title}" was deleted',
        )
        if usage["layouts"]:
            server.hub.broadcast(await layouts_message(server))

    @router.get("/{indicator_id}/usage")
    async def get_usage(indicator_id: str, request: Request) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        definition_file(indicator_id)
        return await usage(indicator_id)

    @router.delete("/{indicator_id}")
    async def delete(
        indicator_id: str,
        request: Request,
        confirm: bool = False,
        alerts: list[str] = Query(default=[]),  # noqa: B008  (FastAPI's way)
    ) -> dict[str, Any]:
        await require_editing(request, server.settings, server.isolation)
        definition_file(indicator_id)
        # The charts' lock, and the file's own: a save of it in progress finishes first.
        async with charts.lock, server.custom.file_lock(indicator_id):
            path = definition_file(indicator_id)  # again: another delete may have run
            found = await usage(indicator_id)
            named = {a["id"] for a in found["alerts"]}
            name = await title(indicator_id)
            if found["in_use"] and (not confirm or set(alerts) != named):
                message = where(found, name)
                if confirm and set(alerts) != named:
                    wrong = sorted(set(alerts) - named)
                    missing = sorted(named - set(alerts))
                    message += (f" Not named: {', '.join(missing)}." if missing else "") + (
                        f" Not on it: {', '.join(wrong)}." if wrong else ""
                    )
                raise HTTPException(409, {"message": message, "usage": found})
            target, history = target_folder(indicator_id)
            # All or nothing (Session A's review of #33): the files move first, each move
            # one rename; if a move or the database step fails, everything goes back.
            moved: list[tuple[Path, Path]] = []
            pairs = [(path, target / path.name)] + (
                [(history, target / "history")] if history is not None else []
            )

            def move_all() -> None:  # in a thread: a move may wait out a reader (Windows)
                for src, dst in pairs:
                    move(src, dst)
                    moved.append((src, dst))

            try:
                await asyncio.to_thread(move_all)
            except OSError as exc:
                await asyncio.to_thread(put_back, moved, target)
                what = "its history" if moved else "the file"
                raise HTTPException(
                    409, f"could not move {what} into .deleted ({exc}); nothing was changed"
                ) from None
            try:
                if found["in_use"]:
                    await remove_instances(indicator_id, found, name)
            except BaseException:
                await asyncio.to_thread(put_back, moved, target)
                raise
        await server.custom.refresh()
        server.hub.broadcast({"type": "indicators_changed", "id": indicator_id, "op": "delete"})
        moved = target.relative_to(server.custom.folder).as_posix()
        return {"id": indicator_id, "moved_to": moved, "removed": found}

    return router
