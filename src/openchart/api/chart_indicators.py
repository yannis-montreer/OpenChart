"""Indicators per chart, checked against their definitions (CI-3, decision D1).

Shared by the chart routes, saved layouts (which carry a copy of a chart's list and put
it back) and deleting a custom definition. Built-in and custom ids are separate
namespaces: every instance has a `kind`. An entry without one (a layout or a browser
list from before CI-3) is the built-in with that id, or the custom one when only a file
of yours has it.

Parameters that no longer fit a definition (an input removed, a choice renamed, a value
out of range) fall back to that input's default, and the instance lists the inputs in
`reset` (the legend's "settings reset" note):

- **on a write** the list is stored as it now fits, and the note is in that write's
  answer and broadcast, once;
- **on a read**, after the definition was edited, the stored values are fitted without
  changing them (editing the file back brings them back), and the note is shown until
  the chart's list is next written.

A write (PUT) refuses a value its input does not accept (400); the import of a browser's
old list and restoring a layout let it fall back instead. An instance of a file of yours
that does not load at the moment (an error, quarantined, the worker not running) keeps
its values as given: they are checked once the file loads again.

All writes to charts' lists go through `lock`, so a check-then-write (the import's "no
list yet", a PUT's revision, a delete's usage) cannot interleave with another.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt

from openchart.indicators._base import Indicator
from openchart.store.chart_indicators import ChartIndicatorStore, Instance

if TYPE_CHECKING:
    from openchart.server import Server

MAX_INDICATORS = 30  # per chart and per layout
MAX_PARAMS = 64  # values per instance
MAX_TEXT = 1000  # characters in a text value (a String input's own limit is at most this)
MAX_WHOLE = 2**53  # integers beyond this are not exact as JSON numbers in a browser
MAX_PARAMS_BYTES = 8192  # one instance's values as JSON (a String input holds up to 1000)

ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
UID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"

# A parameter value as it may be stored: one JSON scalar, bounded. The definition decides
# whether it fits; these bounds hold even when there is no definition to ask. Numbers
# that are not finite (Python's JSON reads NaN and Infinity) are refused by `checked`,
# not here: a validation error echoes its input, and NaN cannot be sent back as JSON.
Value = (
    StrictBool
    | Annotated[StrictInt, Field(ge=-MAX_WHOLE, le=MAX_WHOLE)]
    | StrictFloat
    | Annotated[str, Field(max_length=MAX_TEXT)]
)
Params = Annotated[
    dict[Annotated[str, Field(min_length=1, max_length=40)], Value],
    Field(max_length=MAX_PARAMS),
]


class InstanceIn(BaseModel):
    """One instance as a browser or a layout sends it (other fields, such as `status`
    and `reset` from a read, are ignored)."""

    uid: str | None = Field(None, pattern=UID_PATTERN)
    kind: Literal["builtin", "custom"] | None = None
    id: str = Field(pattern=ID_PATTERN)
    params: Params = Field(default_factory=dict)
    visible: bool = True
    price_labels: bool = True  # last value on the price scale
    legend_values: bool = True  # values in the legend row


InstanceList = Annotated[list[InstanceIn], Field(max_length=MAX_INDICATORS)]


def new_uid() -> str:
    return secrets.token_hex(8)


def fit(indicator: Indicator, given: dict[str, Any], *, strict: bool) -> tuple[dict, list[str]]:
    """Every input of `indicator`, from `given` where it fits. Returns the values and the
    inputs that were reset: unknown names, and (unless `strict`, which raises ValueError
    instead) values the input does not accept."""
    known = {p.name: p for p in indicator.params}
    reset = [name for name in given if name not in known]
    out: dict[str, Any] = {}
    for name, p in known.items():
        if name in given:
            try:
                out[name] = p.check(given[name])
                continue
            except (ValueError, TypeError, OverflowError) as exc:
                if strict:
                    raise ValueError(f"{indicator.name}: {p.to_json()['label']}: {exc}") from None
                reset.append(name)
        out[name] = p.check(p.default)
    return out, sorted(reset)


def bounded(indicator_id: str, params: dict[str, Any]) -> None:
    """422 for a value that is not a finite number, or values too large to keep."""
    for name, value in params.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(422, f"{indicator_id}: {name}: must be a finite number")
    if len(json.dumps(params)) > MAX_PARAMS_BYTES:
        raise HTTPException(
            422, f"{indicator_id}: the values take more than {MAX_PARAMS_BYTES} bytes"
        )


class ChartIndicators:
    def __init__(self, server: Server):
        self.server = server
        self.lock = asyncio.Lock()

    def store(self) -> ChartIndicatorStore:
        return ChartIndicatorStore(self.server.db.conn)

    async def known_chart(self, source: str, instrument: str) -> None:
        """404 unless this server has the chart: its own source and a known instrument."""
        if source != self.server.source:
            raise HTTPException(404, f"unknown source {source!r}")
        if await self.server.store.instrument(source, instrument) is None:
            raise HTTPException(404, f"unknown instrument {instrument!r}")

    async def definition(
        self, kind: str | None, indicator_id: str
    ) -> tuple[str | None, Indicator | None, str]:
        """(kind, schema, status) for an instance. Status `ok` comes with a schema;
        `missing` means no definition has this id in this kind (kind None: in neither);
        otherwise a custom file's status (`error`, `quarantined`, `not_run`)."""
        builtin = self.server.indicators.get(indicator_id)
        if kind is None:
            kind = "builtin" if builtin is not None else "custom"
            if builtin is None and await self.server.custom.get(indicator_id) is None:
                return None, None, "missing"
        if kind == "builtin":
            return kind, builtin, "ok" if builtin is not None else "missing"
        custom = await self.server.custom.get(indicator_id)
        if custom is None:
            return kind, None, "missing"
        if custom.indicator is None:
            return kind, None, custom.status
        return kind, custom.indicator, "ok"

    async def checked(
        self, entries: list[InstanceIn], *, strict: bool
    ) -> tuple[list[Instance], dict[str, list[str]], list[dict[str, Any]]]:
        """The instances to store, the inputs reset in each (by uid), and the entries left
        out. `strict` (PUT, layouts): an unknown definition, a repeated uid or a value an
        input refuses is a 400. Otherwise (import, restore) they are left out, given a new
        uid, or reset."""
        instances: list[Instance] = []
        resets: dict[str, list[str]] = {}
        skipped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for e in entries:
            kind, schema, status = await self.definition(e.kind, e.id)
            if status == "missing":
                what = f"no {kind} indicator {e.id!r}" if kind else f"no indicator {e.id!r}"
                if strict:
                    raise HTTPException(400, what)
                skipped.append({"id": e.id, "kind": kind or e.kind, "reason": what})
                continue
            assert kind is not None
            uid = e.uid or new_uid()
            if uid in seen:
                if strict:
                    raise HTTPException(400, f"the uid {uid!r} is used twice")
                uid = new_uid()
            seen.add(uid)
            params: dict[str, Any] = dict(e.params)
            bounded(e.id, params)
            if schema is not None:
                try:
                    params, reset = fit(schema, params, strict=strict)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from None
                if reset:
                    resets[uid] = reset
            instances.append(
                Instance(uid, kind, e.id, params, e.visible, e.price_labels, e.legend_values)
            )
        return instances, resets, skipped

    async def view(
        self, source: str, instrument: str, resets: dict[str, list[str]] | None = None
    ) -> dict[str, Any]:
        """The chart's list as the API answers it: values fitted to the definitions as they
        are now, with each instance's `status` and `reset`."""
        store = self.store()
        items = []
        for i in await store.list(source, instrument):
            _, schema, status = await self.definition(i.kind, i.indicator)
            params, reset = fit(schema, i.params, strict=False) if schema else (i.params, [])
            reset = sorted({*reset, *(resets or {}).get(i.uid, ())})
            items.append({**i.to_json(), "params": params, "status": status, "reset": reset})
        return {
            "source": source,
            "instrument": instrument,
            "revision": await store.revision(source, instrument),
            "indicators": items,
        }

    async def replace(
        self,
        source: str,
        instrument: str,
        instances: list[Instance],
        resets: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Store the chart's list and tell every browser. Call it holding `lock`."""
        assert self.lock.locked()
        await self.store().replace(source, instrument, instances)
        body = await self.view(source, instrument, resets)
        self.server.hub.broadcast({"type": "chart_indicators_changed", **body})
        return body
