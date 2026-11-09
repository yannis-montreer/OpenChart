"""Indicator alerts in the alert routes: the instance snapshot and every check on it
(docs/designs/indicator-alerts.md sections 1 and 6).

The browser names an instance of the instrument's chart by its `uid`, an output and a
condition; the server fills in the rest from the chart's list and the definition as they
are now (kind, id, value inputs without the style ones, the custom file's hash) and
refuses what does not fit (400), a definition whose outputs cannot be read now (409), and
an instance that is not on the chart (400).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openchart.alerts.indicator_rules import ConditionError, check_condition, warnings_from_checks
from openchart.api.chart_indicators import UID_PATTERN, ChartIndicators, fit
from openchart.store.alerts import IndicatorCondition
from openchart.store.chart_indicators import ChartIndicatorStore

if TYPE_CHECKING:
    from openchart.server import Server

MAX_OUTPUT = 48  # output ids: a name (40) or "background:16"


class IndicatorIn(BaseModel):
    """`indicator` of `POST /api/alerts` with `type: "indicator"`, and of a `PUT`."""

    model_config = ConfigDict(extra="forbid")

    uid: str = Field(pattern=UID_PATTERN)
    output: str = Field(min_length=1, max_length=MAX_OUTPUT)
    condition: Literal["cross_value", "cross_output", "signal"]
    other_output: str | None = Field(None, min_length=1, max_length=MAX_OUTPUT)
    value: float | None = None

    @field_validator("value")
    @classmethod
    def finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("the value must be a finite number")
        return v


async def snapshot(
    server: Server, instrument: str, body: IndicatorIn
) -> tuple[IndicatorCondition, list[str]]:
    """The condition as stored, and the warnings for the alert (from the checks)."""
    instances = await ChartIndicatorStore(server.db.conn).list(server.source, instrument)
    instance = next((i for i in instances if i.uid == body.uid), None)
    if instance is None:
        raise HTTPException(400, f"no indicator {body.uid!r} on the {instrument} chart")
    charts = ChartIndicators(server)
    kind, schema, status = await charts.definition(instance.kind, instance.indicator)
    if schema is None or status != "ok":
        raise HTTPException(
            409, f"{instance.indicator}: its outputs cannot be read now ({status}); fix it first"
        )
    try:
        check_condition(schema, body.output, body.condition, body.other_output, body.value)
    except ConditionError as exc:
        raise HTTPException(400, str(exc)) from None
    settings, _ = fit(schema, instance.params, strict=False)
    style = {p.name for p in schema.params if p.group == "style"}
    digest, warnings = "builtin", []
    if kind == "custom":
        custom = await server.custom.get(instance.indicator)
        assert custom is not None and custom.digest is not None  # status ok
        digest = custom.digest
        warnings = warnings_from_checks(server.custom.checks_for(custom, settings))
    cond = IndicatorCondition(
        uid=instance.uid,
        kind=kind or instance.kind,
        id=instance.indicator,
        params={k: v for k, v in settings.items() if k not in style},
        digest=digest,
        output=body.output,
        condition=body.condition,
        other_output=body.other_output,
        value=body.value,
        name=schema.name,
    )
    return cond, warnings
