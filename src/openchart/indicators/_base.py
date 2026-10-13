"""The indicator interface (SPEC section 11). See docs/indicators.md for how to write one.

An indicator is one Python file. It declares, at module level:

- `NAME` (short, for the legend, "EMA") and `TITLE` (for the menu)
- `OVERLAY`: True to draw on the price pane, False for a pane of its own
- `PARAMS`: a list of parameters (`Int`, `Float`, `Bool`, `Choice`, `Source`, `Color`,
  `Width`), each with a default. Color and width parameters appear on the Style tab of the
  settings dialog, the others on the Inputs tab.
- `OUTPUTS`: the lines it draws, each naming the color and width parameters it uses
- `compute(bars, params)`: returns `{output name: series}`, one value per bar (NaN where
  there is none yet)
- optionally `warmup(params)`: how many bars before the first one shown it needs to give
  settled values (default 0)

`bars` is a DataFrame with `ts, o, h, l, c, v`, oldest first, in the price type the chart
shows (bid, mid or ask). `params` holds every parameter, validated, defaults filled in.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "hlcc4")
STYLE_TYPES = ("color", "width")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # int | float | bool | choice | source | color | width
    default: Any
    label: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()

    @property
    def group(self) -> str:
        """The settings tab it belongs on."""
        return "style" if self.type in STYLE_TYPES else "inputs"

    def check(self, value: Any) -> Any:
        """The value, validated and normalized; ValueError if it is not acceptable."""
        t = self.type
        if t == "bool":
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        if t in ("int", "width"):
            if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
                raise ValueError("must be a whole number")
            value = int(value)
        elif t == "float":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("must be a number")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("must be a finite number")
        elif t in ("choice", "source"):
            if value not in self.options:
                raise ValueError(f"must be one of {list(self.options)}")
            return value
        elif t == "color":
            if not isinstance(value, str) or not _COLOR.match(value):
                raise ValueError("must be a colour like #2962ff")
            return value.lower()
        else:
            raise ValueError(f"unknown parameter type {t!r}")
        if self.min is not None and value < self.min:
            raise ValueError(f"must be at least {self.min:g}")
        if self.max is not None and value > self.max:
            raise ValueError(f"must be at most {self.max:g}")
        return value

    @property
    def options(self) -> tuple[str, ...]:
        return SOURCES if self.type == "source" else self.choices

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "label": self.label or self.name.replace("_", " ").capitalize(),
            "group": self.group,
        }
        for key in ("min", "max", "step"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.options:
            out["choices"] = list(self.options)
        return out


def Int(
    name: str,
    default: int,
    *,
    min: int | None = None,
    max: int | None = None,
    label: str | None = None,
) -> Param:
    return Param(name, "int", default, label, min, max, 1)


def Float(
    name: str,
    default: float,
    *,
    min: float | None = None,
    max: float | None = None,
    step: float | None = None,
    label: str | None = None,
) -> Param:
    return Param(name, "float", default, label, min, max, step)


def Bool(name: str, default: bool, *, label: str | None = None) -> Param:
    return Param(name, "bool", default, label)


def Choice(
    name: str, default: str, choices: list[str] | tuple[str, ...], *, label: str | None = None
) -> Param:
    return Param(name, "choice", default, label, choices=tuple(choices))


def Source(name: str = "source", default: str = "close", *, label: str | None = None) -> Param:
    """Which price of each bar: open, high, low, close, hl2, hlc3, ohlc4 or hlcc4."""
    return Param(name, "source", default, label)


def Color(name: str, default: str, *, label: str | None = None) -> Param:
    return Param(name, "color", default, label)


def Width(name: str, default: int = 1, *, label: str | None = None) -> Param:
    """Line width in pixels, 1 to 4."""
    return Param(name, "width", default, label, 1, 4, 1)


@dataclass(frozen=True)
class Output:
    """A line the indicator draws. `color` and `width` name parameters of the indicator."""

    name: str
    label: str
    color: str
    width: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "color": self.color, "width": self.width}


Compute = Callable[[pd.DataFrame, dict[str, Any]], Mapping[str, Any]]


@dataclass
class Indicator:
    id: str  # the file name without .py
    name: str
    title: str
    overlay: bool
    params: tuple[Param, ...]
    outputs: tuple[Output, ...]
    compute: Compute
    warmup: Callable[[dict[str, Any]], int] = field(default=lambda _p: 0)
    path: Path | None = None
    builtin: bool = True

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def normalize(self, given: Mapping[str, Any] | None) -> dict[str, Any]:
        """Every parameter, validated, with defaults for those not given."""
        given = dict(given or {})
        known = {p.name: p for p in self.params}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise ValueError(f"{self.name} has no parameter {unknown[0]!r}")
        out = {}
        for name, p in known.items():
            try:
                out[name] = p.check(given.get(name, p.default))
            except ValueError as exc:
                raise ValueError(f"{p.to_json()['label']}: {exc}") from None
        return out

    def run(self, bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, pd.Series]:
        """`compute`, checked: one float series per output, as long as `bars`."""
        result = self.compute(bars, params)
        out = {}
        for o in self.outputs:
            if o.name not in result:
                raise ValueError(f"{self.name} returned no {o.name!r} output")
            values = pd.Series(result[o.name], dtype="float64").reset_index(drop=True)
            if len(values) != len(bars):
                raise ValueError(
                    f"{self.name} output {o.name!r} has {len(values)} values for {len(bars)} bars"
                )
            out[o.name] = values
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "overlay": self.overlay,
            "params": [p.to_json() for p in self.params],
            "outputs": [o.to_json() for o in self.outputs],
            "builtin": self.builtin,
        }


def source(bars: pd.DataFrame, name: str) -> pd.Series:
    """The source price of each bar (see SOURCES)."""
    o, h, low, c = bars["o"], bars["h"], bars["l"], bars["c"]
    series = {
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "hl2": (h + low) / 2,
        "hlc3": (h + low + c) / 3,
        "ohlc4": (o + h + low + c) / 4,
        "hlcc4": (h + low + 2 * c) / 4,
    }
    if name not in series:
        raise ValueError(f"unknown source {name!r}")
    return series[name].astype("float64").reset_index(drop=True)
