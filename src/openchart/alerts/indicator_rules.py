"""Rules of indicator alerts that need no server (docs/designs/indicator-alerts.md).

- Which outputs a condition may use: a crossing (`cross_value`, `cross_output`) needs an
  output with numbers (line, histogram, area); `cross_output`'s other output may also be a
  level; `signal` needs a marker or a background.
- The warnings shown on an alert, from the custom file's save-time checks.
- The default name.
"""

from __future__ import annotations

import math
from typing import Any

from openchart.indicators._base import Indicator
from openchart.store.alerts import IndicatorCondition

NUMBER_KINDS = ("line", "histogram", "area")
SIGNAL_KINDS = ("marker", "background")


class ConditionError(ValueError):
    """A condition that does not fit the indicator's outputs (a 400)."""


def outputs_by_id(indicator: Indicator) -> dict[str, Any]:
    return dict(zip(indicator.output_ids(), indicator.outputs, strict=True))


def check_condition(
    indicator: Indicator,
    output: str,
    condition: str,
    other_output: str | None,
    value: float | None,
) -> None:
    """ConditionError unless the condition fits `indicator`'s outputs."""
    outputs = outputs_by_id(indicator)
    name = indicator.name
    if output not in outputs:
        raise ConditionError(f"{name} has no output {output!r} (it has {sorted(outputs)})")
    kind = outputs[output].kind
    if condition == "signal":
        if kind not in SIGNAL_KINDS:
            raise ConditionError(
                f"{name}: {output!r} is a {kind}; a signal alert needs a marker or a background"
            )
        if other_output is not None or value is not None:
            raise ConditionError("a signal alert takes no value and no other output")
        return
    if kind not in NUMBER_KINDS:
        raise ConditionError(
            f"{name}: {output!r} is a {kind}; a crossing needs a line, histogram or area"
        )
    if condition == "cross_value":
        if value is None or not math.isfinite(value):
            raise ConditionError("crossing a value needs a finite number in 'value'")
        if other_output is not None:
            raise ConditionError("crossing a value takes no other output")
        return
    if condition == "cross_output":
        if value is not None:
            raise ConditionError("crossing another output takes no 'value'")
        if other_output is None:
            raise ConditionError("crossing another output needs 'other_output'")
        if other_output == output:
            raise ConditionError("an output cannot cross itself")
        if other_output not in outputs:
            raise ConditionError(f"{name} has no output {other_output!r}")
        other = outputs[other_output].kind
        if other not in (*NUMBER_KINDS, "level"):
            raise ConditionError(
                f"{name}: {other_output!r} is a {other};"
                " it must be a line, histogram, area or level"
            )
        return
    raise ConditionError(f"unknown condition {condition!r}")


def output_exists(indicator: Indicator, cond: IndicatorCondition) -> str | None:
    """Why `cond` no longer fits `indicator` (an output gone or of another kind), or None."""
    try:
        check_condition(indicator, cond.output, cond.condition, cond.other_output, cond.value)
    except ConditionError as exc:
        return str(exc)
    return None


def warnings_from_checks(checks: dict[str, Any] | None) -> list[str]:
    """The alert's badges from a custom file's save-time checks (`CustomIndicators.checks_for`):
    warnings never block an alert (D4)."""
    if checks is None:
        return ["unchecked"]
    out = []
    if checks.get("lookahead") == "warned":
        out.append("lookahead")
    if checks.get("warmup") == "warned":
        out.append("warmup_short")
    if any(checks.get(k) in ("pending", "not_checked") for k in ("lookahead", "warmup")):
        out.append("unchecked")
    return out


def default_name(instrument: str, timeframe: str, direction: str, cond: IndicatorCondition) -> str:
    what = {"up": "crossing up", "down": "crossing down"}.get(direction, "crossing")
    if cond.condition == "signal":
        return f"{instrument}, {timeframe} {cond.name} {cond.output} signal"
    target = f"{cond.value:g}" if cond.condition == "cross_value" else cond.other_output
    return f"{instrument}, {timeframe} {cond.name} {cond.output} {what} {target}"
