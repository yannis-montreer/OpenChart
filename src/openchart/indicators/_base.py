"""The indicator interface (SPEC section 11). See docs/indicators.md for how to write one.

An indicator is one Python file. It declares, at module level:

- `NAME` (short, for the legend, "EMA") and `TITLE` (for the menu)
- `OVERLAY`: True to draw on the price pane, False for a pane of its own
- `PARAMS`: the inputs (`Int`, `Float`, `Bool`, `Choice`, `String`, `Source`, `Color`,
  `Width`), each with a default. Color and width inputs appear on the Style tab of the
  settings dialog, the others on the Inputs tab.
- `OUTPUTS`: what it draws: `Line`, `Histogram`, `Area`, `Fill`, `Level`, `Marker` and
  `Background`, each naming the colour (and width) inputs it uses
- `compute(bars, params)` or `compute(bars, params, info)`: returns `{output name:
  values}`, one value per bar (NaN where there is none yet; True/False for markers and
  backgrounds)
- optionally `warmup(params)`: how many bars before the first one shown it needs to give
  settled values (default 0)

`bars` is a DataFrame with `ts, o, h, l, c, v` (and the long names `time, open, high, low,
close, volume`), oldest first, in the price type the chart shows (bid, mid or ask).
`params` holds every input, validated, defaults filled in. `info` (only passed to a
`compute` that declares a third argument) says which instrument and timeframe this is.

What a file declares is checked when it loads (`validate_schema`: every failure names the
file, the input or output, and the rule), and what `compute` returns is checked every time
(`Indicator.run` in the process that ran the script, `Indicator.check_wire` again in the
server for what a worker sent back).
"""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd

SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "hlcc4")
STYLE_TYPES = ("color", "width")
PARAM_TYPES = ("int", "float", "bool", "choice", "string", "source", "color", "width")
LINE_STYLES = ("solid", "dashed", "dotted")
MARKER_SHAPES = ("arrow_up", "arrow_down", "circle", "square")
MARKER_POSITIONS = ("above", "below", "on")

MAX_INPUTS = 30
MAX_OUTPUTS = 16
MAX_LABEL = 60
MAX_FILE_BYTES = 256 * 1024
MAX_STRING_LENGTH = 1000

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


class SchemaError(ValueError):
    """A file whose declarations break a rule (raised while it loads)."""


class ResultError(ValueError):
    """Values from `compute` that cannot be drawn."""


# Inputs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # int | float | bool | choice | string | source | color | width
    default: Any
    label: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    max_length: int | None = None  # string only

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
        if t == "string":
            if not isinstance(value, str):
                raise ValueError("must be text")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"must be at most {self.max_length} characters")
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
        for key in ("min", "max", "step", "max_length"):
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


def String(name: str, default: str, max_length: int = 100, *, label: str | None = None) -> Param:
    """Free text, for example a label. At most `max_length` characters (1 to 1000)."""
    return Param(name, "string", default, label, max_length=max_length)


def Source(name: str = "source", default: str = "close", *, label: str | None = None) -> Param:
    """Which price of each bar: open, high, low, close, hl2, hlc3, ohlc4 or hlcc4."""
    return Param(name, "source", default, label)


def Color(name: str, default: str, *, label: str | None = None) -> Param:
    return Param(name, "color", default, label)


def Width(name: str, default: int = 1, *, label: str | None = None) -> Param:
    """Line width in pixels, 1 to 4."""
    return Param(name, "width", default, label, 1, 4, 1)


# Outputs ---------------------------------------------------------------------------------
# Colour and width are the names of Color and Width inputs, so the Style tab can change them.


@dataclass(frozen=True)
class Line:
    """A line: one float per bar, NaN is a gap. (`Output` is the M7 name for it.)"""

    name: str
    label: str
    color: str
    width: str
    style: str = "solid"  # solid | dashed | dotted

    kind: ClassVar[str] = "line"
    data: ClassVar[str | None] = "number"

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        return {
            "kind": "line",
            "name": self.name,
            "label": self.label,
            "color": self.color,
            "width": self.width,
            "style": self.style,
        }


Output = Line


@dataclass(frozen=True)
class Histogram:
    """Columns from `base` to a float per bar."""

    name: str
    label: str
    _: KW_ONLY
    color: str
    base: float = 0

    kind: ClassVar[str] = "histogram"
    data: ClassVar[str | None] = "number"

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        base = float(self.base) if _is_number(self.base) else self.base
        return {
            "kind": "histogram",
            "name": self.name,
            "label": self.label,
            "color": self.color,
            "base": base,
        }


@dataclass(frozen=True)
class Area:
    """A line with a shaded area below it: a float per bar."""

    name: str
    label: str
    _: KW_ONLY
    color: str
    width: str | None = None

    kind: ClassVar[str] = "area"
    data: ClassVar[str | None] = "number"

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        return {
            "kind": "area",
            "name": self.name,
            "label": self.label,
            "color": self.color,
            "width": self.width,
        }


@dataclass(frozen=True)
class Fill:
    """The band between two other outputs (lines or areas). Takes no values of its own."""

    between: tuple[str, str]
    _: KW_ONLY
    color: str
    opacity: float = 0.15
    label: str = "Fill"

    kind: ClassVar[str] = "fill"
    data: ClassVar[str | None] = None

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        between = list(self.between) if isinstance(self.between, tuple | list) else self.between
        opacity = float(self.opacity) if _is_number(self.opacity) else self.opacity
        return {
            "kind": "fill",
            "name": oid,
            "label": self.label,
            "between": between,
            "color": self.color,
            "opacity": opacity,
        }


@dataclass(frozen=True)
class Level:
    """A horizontal line at a number, or at the value of a Float input. No values."""

    value: float | str
    label: str = "Level"
    _: KW_ONLY
    color: str
    width: str | None = None
    style: str = "dashed"

    kind: ClassVar[str] = "level"
    data: ClassVar[str | None] = None

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        named = isinstance(self.value, str)
        return {
            "kind": "level",
            "name": oid,
            "label": self.label,
            "value": None if named else float(self.value),
            "input": self.value if named else None,
            "color": self.color,
            "width": self.width,
            "style": self.style,
        }


@dataclass(frozen=True)
class Marker:
    """A shape on the bars where `compute` returns True."""

    name: str
    label: str
    _: KW_ONLY
    color: str
    shape: str = "circle"  # arrow_up | arrow_down | circle | square
    position: str = "above"  # above | below | on (the bar's close)
    text: str = ""

    kind: ClassVar[str] = "marker"
    data: ClassVar[str | None] = "bool"

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        return {
            "kind": "marker",
            "name": self.name,
            "label": self.label,
            "color": self.color,
            "shape": self.shape,
            "position": self.position,
            "text": self.text,
        }


@dataclass(frozen=True)
class Background:
    """The background of the bars where `compute` returns True, tinted."""

    name: str
    label: str
    _: KW_ONLY
    color: str
    opacity: float = 0.15

    kind: ClassVar[str] = "background"
    data: ClassVar[str | None] = "bool"

    def to_json(self, oid: str | None = None) -> dict[str, Any]:
        opacity = float(self.opacity) if _is_number(self.opacity) else self.opacity
        return {
            "kind": "background",
            "name": self.name,
            "label": self.label,
            "color": self.color,
            "opacity": opacity,
        }


OUTPUT_TYPES = (Line, Histogram, Area, Fill, Level, Marker, Background)
OutputSpec = Line | Histogram | Area | Fill | Level | Marker | Background
_BY_KIND = {t.kind: t for t in OUTPUT_TYPES}


def output_ids(outputs: tuple[OutputSpec, ...] | list[OutputSpec]) -> list[str]:
    """The id of each output in the browser's JSON and in `compute`'s result: its name, and
    for the two kinds that have none, `fill:1`, `fill:2`, `level:1` ... (not identifiers, so
    they cannot clash with a name)."""
    counts: dict[str, int] = {}
    ids = []
    for o in outputs:
        if o.data is None:
            counts[o.kind] = counts.get(o.kind, 0) + 1
            ids.append(f"{o.kind}:{counts[o.kind]}")
        else:
            ids.append(o.name)  # type: ignore[union-attr]
    return ids


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def output_from_json(d: Any) -> OutputSpec:
    """An output as a worker described it (`to_json`). ValueError if it is not one."""
    if not isinstance(d, dict) or d.get("kind", "line") not in _BY_KIND:
        raise ValueError(f"unknown output {str(d)[:80]}")
    kind = d.get("kind", "line")
    try:
        if kind == "line":
            return Line(d["name"], d["label"], d["color"], d["width"], d.get("style", "solid"))
        if kind == "histogram":
            return Histogram(d["name"], d["label"], color=d["color"], base=d.get("base", 0))
        if kind == "area":
            return Area(d["name"], d["label"], color=d["color"], width=d.get("width"))
        if kind == "fill":
            return Fill(
                tuple(d["between"]) if isinstance(d["between"], list) else d["between"],
                color=d["color"],
                opacity=d.get("opacity", 0.15),
                label=d.get("label", "Fill"),
            )
        if kind == "level":
            value = d["input"] if d.get("input") is not None else d["value"]
            return Level(
                value,
                d.get("label", "Level"),
                color=d["color"],
                width=d.get("width"),
                style=d.get("style", "dashed"),
            )
        if kind == "marker":
            return Marker(
                d["name"],
                d["label"],
                color=d["color"],
                shape=d.get("shape", "circle"),
                position=d.get("position", "above"),
                text=d.get("text", ""),
            )
        return Background(d["name"], d["label"], color=d["color"], opacity=d.get("opacity", 0.15))
    except KeyError as exc:
        raise ValueError(f"{kind} output without {exc.args[0]!r}") from None


# The schema ------------------------------------------------------------------------------


def validate_schema(
    file: str,
    name: Any,
    title: Any,
    params: tuple | list,
    outputs: tuple | list,
) -> None:
    """Every rule of design 2.1 and 2.3. SchemaError names the file, the input or output,
    and the rule."""

    def bad(where: str, rule: str) -> SchemaError:
        return SchemaError(f"{file}: {where}: {rule}")

    def top(rule: str) -> SchemaError:
        return SchemaError(f"{file}: {rule}")

    for label, text in (("NAME", name), ("TITLE", title)):
        if not isinstance(text, str) or not text.strip():
            raise top("NAME and TITLE must be text")
        if len(text) > MAX_LABEL:
            raise top(f"{label} must be at most {MAX_LABEL} characters (it has {len(text)})")

    if not all(isinstance(p, Param) for p in params):
        raise top(
            "PARAMS must hold Int(...), Float(...), Bool(...), Choice(...), String(...), "
            "Source(...), Color(...) and Width(...)"
        )
    if not outputs:
        raise top("OUTPUTS must hold at least one output: Line(...), Histogram(...) and the like")
    if not all(isinstance(o, OUTPUT_TYPES) for o in outputs):
        raise top(
            "OUTPUTS must hold Line(...), Histogram(...), Area(...), Fill(...), Level(...), "
            "Marker(...) and Background(...)"
        )
    if len(params) > MAX_INPUTS:
        raise top(f"{len(params)} inputs; at most {MAX_INPUTS}")
    if len(outputs) > MAX_OUTPUTS:
        raise top(f"{len(outputs)} outputs; at most {MAX_OUTPUTS}")

    by_name: dict[str, Param] = {}
    for p in params:
        where = f"input {p.name!r}"
        if not isinstance(p.name, str) or not _IDENT.match(p.name):
            raise bad(
                where,
                "the name must be an identifier (letters, digits and _, starting with a "
                "letter or _, at most 40 characters)",
            )
        if p.name in by_name:
            raise bad(where, "name used twice")
        by_name[p.name] = p
        _check_label(bad, where, p.label)
        _check_param(bad, where, p)

    names: dict[str, OutputSpec] = {}
    for o in outputs:
        if o.data is None:
            continue
        where = f"output {o.name!r}"  # type: ignore[union-attr]
        if not isinstance(o.name, str) or not _IDENT.match(o.name):  # type: ignore[union-attr]
            raise bad(
                where,
                "the name must be an identifier (letters, digits and _, starting with a "
                "letter or _, at most 40 characters)",
            )
        if o.name in names:  # type: ignore[union-attr]
            raise bad(where, "name used twice")
        names[o.name] = o  # type: ignore[union-attr]

    counts: dict[str, int] = {}
    for o in outputs:
        if o.data is None:
            counts[o.kind] = counts.get(o.kind, 0) + 1
            where = f"{o.kind.capitalize()} #{counts[o.kind]}"
        else:
            where = f"output {o.name!r}"  # type: ignore[union-attr]
        _check_output(bad, where, o, by_name, names)


def _check_label(bad: Callable[[str, str], SchemaError], where: str, label: Any) -> None:
    if label is None:
        return
    if not isinstance(label, str):
        raise bad(where, "label must be text")
    if len(label) > MAX_LABEL:
        raise bad(where, f"label must be at most {MAX_LABEL} characters (it has {len(label)})")


def _number(v: Any) -> bool:
    return _is_number(v) and math.isfinite(v)


def _check_param(bad: Callable[[str, str], SchemaError], where: str, p: Param) -> None:
    if p.type not in PARAM_TYPES:
        raise bad(where, f"unknown input type {p.type!r}")
    if p.type in ("int", "float"):
        for key in ("min", "max"):
            v = getattr(p, key)
            if v is not None and not _number(v):
                raise bad(where, f"{key} must be a number")
        if p.min is not None and p.max is not None and p.min > p.max:
            raise bad(where, f"min {p.min:g} is greater than max {p.max:g}")
        if p.step is not None and not (_number(p.step) and p.step > 0):
            raise bad(where, "step must be above 0")
    if p.type == "choice":
        if not isinstance(p.choices, tuple | list) or len(p.choices) == 0:
            raise bad(where, "needs at least one choice")
        if not all(isinstance(c, str) for c in p.choices):
            raise bad(where, "choices must be text")
        if len(set(p.choices)) != len(p.choices):
            raise bad(where, "choices are repeated")
    if p.type == "string" and not (
        isinstance(p.max_length, int)
        and not isinstance(p.max_length, bool)
        and 1 <= p.max_length <= MAX_STRING_LENGTH
    ):
        raise bad(where, f"max_length must be a whole number between 1 and {MAX_STRING_LENGTH}")
    try:
        p.check(p.default)
    except ValueError as exc:
        raise bad(where, f"default {p.default!r} {exc}") from None


def _check_ref(
    bad: Callable[[str, str], SchemaError],
    where: str,
    ref: Any,
    role: str,
    by_name: dict[str, Param],
) -> None:
    found = by_name.get(ref) if isinstance(ref, str) else None
    if found is None or found.type != role:
        raise bad(where, f"{ref!r} is not a {role} parameter (name a {role.capitalize()} input)")


def _check_choice(
    bad: Callable[[str, str], SchemaError], where: str, what: str, value: Any, allowed: tuple
) -> None:
    if value not in allowed:
        raise bad(where, f"{what} must be {', '.join(allowed[:-1])} or {allowed[-1]}")


def _check_opacity(bad: Callable[[str, str], SchemaError], where: str, value: Any) -> None:
    if not (_number(value) and 0 < value <= 1):
        raise bad(where, "opacity must be above 0 and at most 1")


def _check_output(
    bad: Callable[[str, str], SchemaError],
    where: str,
    o: OutputSpec,
    by_name: dict[str, Param],
    names: dict[str, OutputSpec],
) -> None:
    _check_label(bad, where, o.label)
    _check_ref(bad, where, o.color, "color", by_name)
    if isinstance(o, Line):
        _check_ref(bad, where, o.width, "width", by_name)
        _check_choice(bad, where, "style", o.style, LINE_STYLES)
    elif isinstance(o, Histogram):
        if not _number(o.base):
            raise bad(where, "base must be a number")
    elif isinstance(o, Area):
        if o.width is not None:
            _check_ref(bad, where, o.width, "width", by_name)
    elif isinstance(o, Fill):
        pair = o.between
        if not (isinstance(pair, tuple | list) and len(pair) == 2):
            raise bad(where, "between must be two output names, like between=('upper', 'lower')")
        for ref in pair:
            found = names.get(ref) if isinstance(ref, str) else None
            if found is None:
                raise bad(where, f"between {ref!r}: no such output")
            if found.kind not in ("line", "area"):
                raise bad(where, f"between {ref!r}: a {found.kind}; Fill needs a line or area")
        if pair[0] == pair[1]:
            raise bad(where, "between needs two different outputs")
        _check_opacity(bad, where, o.opacity)
    elif isinstance(o, Level):
        if o.width is not None:
            _check_ref(bad, where, o.width, "width", by_name)
        _check_choice(bad, where, "style", o.style, LINE_STYLES)
        if isinstance(o.value, str):
            found = by_name.get(o.value)
            if found is None:
                raise bad(where, f"value {o.value!r}: no such input")
            if found.type != "float":
                raise bad(where, f"value {o.value!r} is not a Float input")
        elif not _is_number(o.value):
            raise bad(where, "value must be a number or the name of a Float input")
        elif not math.isfinite(o.value):
            raise bad(where, "value must be a finite number")
    elif isinstance(o, Marker):
        _check_choice(bad, where, "shape", o.shape, MARKER_SHAPES)
        _check_choice(bad, where, "position", o.position, MARKER_POSITIONS)
        if not isinstance(o.text, str) or len(o.text) > MAX_LABEL:
            raise bad(where, f"text must be text of at most {MAX_LABEL} characters")
    elif isinstance(o, Background):
        _check_opacity(bad, where, o.opacity)


# What compute receives -------------------------------------------------------------------


@dataclass(frozen=True)
class Info:
    """Which instrument and timeframe `compute` runs for (its optional third argument)."""

    instrument: str  # "XAU_USD"
    display_name: str  # "Gold"
    decimals: int  # price digits shown for it
    timeframe: str  # "1h"
    price: str  # "mid", "bid" or "ask"

    def to_json(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "display_name": self.display_name,
            "decimals": self.decimals,
            "timeframe": self.timeframe,
            "price": self.price,
        }

    @classmethod
    def from_json(cls, d: Any) -> Info:
        fields = ("instrument", "display_name", "decimals", "timeframe", "price")
        if not isinstance(d, dict) or set(d) != set(fields):
            raise ValueError(f"info must have exactly {list(fields)}")
        for key in fields:
            ok = (
                isinstance(d[key], int) and not isinstance(d[key], bool)
                if key == "decimals"
                else isinstance(d[key], str)
            )
            if not ok:
                raise ValueError(f"info.{key} has the wrong type")
        return cls(**d)


LONG_NAMES = {
    "time": "ts",
    "open": "o",
    "high": "h",
    "low": "l",
    "close": "c",
    "volume": "v",
}


def with_long_names(bars: pd.DataFrame) -> pd.DataFrame:
    """The bars with `time, open, high, low, close, volume` beside `ts, o, h, l, c, v`
    (the caller's frame is not changed)."""
    return bars.assign(**{long: bars[short] for long, short in LONG_NAMES.items()})


def _takes_info(fn: Callable) -> bool:
    """Does `compute` declare a third argument (or *args)?"""
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = 0
    for p in parameters:
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 3


Compute = Callable[..., Mapping[str, Any]]


# What compute returns --------------------------------------------------------------------


def _describe(value: Any) -> str:
    if value is None or isinstance(value, bool | int | float | str | bytes | np.generic):
        return "a single value"
    return f"a {type(value).__name__}"


def _coerce(name: str, value: Any, n: int, boolean: bool) -> np.ndarray:
    """One output's values as float64 (a boolean as 0/1), or ResultError."""
    what = f"output {name!r}"
    if isinstance(value, np.ndarray):
        if value.ndim != 1:
            raise ResultError(f"{what} must be one-dimensional: one value per bar")
        count = len(value)
    elif isinstance(value, pd.Series | list | tuple):
        count = len(value)
    else:
        raise ResultError(
            f"{what} must be a series with one value per bar (a pandas Series, numpy array "
            f"or list), not {_describe(value)}"
        )
    if count != n:
        raise ResultError(f"{what} has {count:,} values for {n:,} bars")
    wanted = "true/false values" if boolean else "numbers"
    if isinstance(value, pd.Series):
        dtype = value.dtype
        if pd.api.types.is_extension_array_dtype(dtype):
            if pd.api.types.is_bool_dtype(dtype):
                if value.isna().any():
                    raise ResultError(f"{what} must be true/false values: it has gaps")
                arr = value.to_numpy(dtype=bool)
            elif pd.api.types.is_numeric_dtype(dtype) and not boolean:
                arr = value.to_numpy(dtype="float64", na_value=np.nan)
            else:
                raise ResultError(f"{what} must be {wanted}, not {dtype}")
        else:
            arr = value.to_numpy()
    elif isinstance(value, np.ndarray):
        arr = value
    else:
        try:
            arr = np.asarray(value)
        except (ValueError, TypeError):
            raise ResultError(f"{what} must be {wanted}") from None
    if arr.ndim != 1:
        raise ResultError(f"{what} must be one-dimensional: one value per bar")
    if boolean:
        if arr.dtype.kind != "b":
            raise ResultError(
                f"{what} must be true/false values (a comparison such as bars.close > bars.open, "
                f"or ta.crossover(...)), not {_kind_name(arr)}; numbers and gaps are not allowed"
            )
        return arr.astype("float64")
    if arr.dtype.kind == "b":
        raise ResultError(f"{what} must be numbers, not true/false values")
    if arr.dtype.kind == "O":
        if not all(v is None or (_is_number(v) or isinstance(v, np.number)) for v in arr):
            raise ResultError(f"{what} must be numbers (None is allowed as a gap)")
        arr = np.array([np.nan if v is None else v for v in arr], dtype="float64")
    elif arr.dtype.kind not in "iuf":
        raise ResultError(f"{what} must be numbers, not {_kind_name(arr)}")
    arr = arr.astype("float64")
    return np.where(np.isfinite(arr), arr, np.nan)


def _kind_name(arr: np.ndarray) -> str:
    return {"U": "text", "S": "text", "O": "mixed values", "f": "numbers", "i": "numbers"}.get(
        arr.dtype.kind, f"{arr.dtype} values"
    )


@dataclass
class Indicator:
    id: str  # the file name without .py
    name: str
    title: str
    overlay: bool
    params: tuple[Param, ...]
    outputs: tuple[OutputSpec, ...]
    compute: Compute
    warmup: Callable[[dict[str, Any]], int] = field(default=lambda _p: 0)
    path: Path | None = None
    builtin: bool = True

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def output_ids(self) -> list[str]:
        return output_ids(self.outputs)

    def data_outputs(self) -> list[tuple[str, OutputSpec]]:
        """(id, output) of the outputs `compute` returns values for."""
        return [
            (oid, o)
            for oid, o in zip(self.output_ids(), self.outputs, strict=True)
            if o.data is not None
        ]

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

    def run(
        self, bars: pd.DataFrame, params: dict[str, Any], info: Info | None = None
    ) -> dict[str, pd.Series]:
        """`compute`, checked: one float series per value-carrying output, as long as `bars`
        (a boolean output is 0.0/1.0). ResultError for anything that cannot be drawn."""
        frame = with_long_names(bars)
        result = (
            self.compute(frame, params, info)
            if _takes_info(self.compute)
            else self.compute(frame, params)
        )
        if not isinstance(result, Mapping):
            raise ResultError("compute must return a dict {output name: values}")
        wanted = self.data_outputs()
        known = {oid for oid, _ in wanted}
        by_id = dict(zip(self.output_ids(), self.outputs, strict=True))
        for key in result:
            if key not in known:
                if isinstance(key, str) and key in by_id:
                    raise ResultError(
                        f"compute returned {key!r}, but a {by_id[key].kind} takes no values"
                    )
                raise ResultError(f"compute returned {key!r}, which is not a declared output")
        out = {}
        for oid, o in wanted:
            if oid not in result:
                raise ResultError(f"compute returned no {oid!r} output")
            out[oid] = pd.Series(_coerce(oid, result[oid], len(bars), o.data == "bool"))
        return out

    def check_wire(self, arrays: Mapping[str, Any], n: int) -> dict[str, pd.Series]:
        """What a worker sent back (arrays by output id), checked again here: the worker is
        not trusted. Exactly the declared value-carrying outputs, float64 vectors of `n`
        values, 0/1 for markers and backgrounds."""
        wanted = self.data_outputs()
        known = {oid for oid, _ in wanted}
        by_id = dict(zip(self.output_ids(), self.outputs, strict=True))
        for key in arrays:
            if key not in known:
                if key in by_id:
                    raise ResultError(
                        f"the worker returned {key!r}, but a {by_id[key].kind} takes no values"
                    )
                raise ResultError(f"the worker returned {key!r}, which is not a declared output")
        out = {}
        for oid, o in wanted:
            if oid not in arrays:
                raise ResultError(f"compute returned no {oid!r} output")
            a = arrays[oid]
            what = f"output {oid!r}"
            if not (isinstance(a, np.ndarray) and a.ndim == 1 and a.dtype == np.dtype("float64")):
                raise ResultError(f"{what} is not a vector of numbers")
            if len(a) != n:
                raise ResultError(f"{what} has {len(a):,} values for {n:,} bars")
            if o.data == "bool":
                if not np.all((a == 0) | (a == 1)):
                    raise ResultError(f"{what} must be true/false values")
                out[oid] = pd.Series(a)
            else:
                out[oid] = pd.Series(np.where(np.isfinite(a), a, np.nan))
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "overlay": self.overlay,
            "params": [p.to_json() for p in self.params],
            "outputs": [
                o.to_json(oid) for oid, o in zip(self.output_ids(), self.outputs, strict=True)
            ],
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
