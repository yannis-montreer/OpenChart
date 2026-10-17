"""Shape rules of the drawing tools (docs/decisions-drawing-tools.md).

What a drawing's anchors must satisfy, checked by the routes on create and on every
change. `frontend/src/drawings/shapes.ts` (`shapeError`) has the same rules; both run
`frontend/src/drawings/shape-rules.fixture.json`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

# type -> number of anchors
TYPES = {
    "hline": 1,
    "trendline": 2,
    "ray": 2,
    "extended": 2,
    "vline": 1,
    "text": 1,
    "rect": 2,
    "fib": 2,
    "measure": 2,
    "channel": 3,
}
# The types an alert can follow (the alert engine knows their geometry).
LINE_TYPES = ("hline", "trendline", "ray", "extended")

Clock = Callable[[float], float]


def channel_offset(points: Sequence[dict[str, Any]], clock: Clock | None = None) -> float:
    """Price distance of a channel's third anchor from its base line, which is extended
    to every time and measured in trading time, so the second line is parallel to the
    first on the chart and for the alert engine."""
    clock = clock or float
    (a, b, c) = points
    span = clock(b["t"]) - clock(a["t"])
    base = a["p"] + (b["p"] - a["p"]) * (clock(c["t"]) - clock(a["t"])) / span
    return c["p"] - base


def shape_error(
    type_: str,
    points: Sequence[dict[str, Any]],
    clock: Clock | None = None,
    label: str | None = None,
) -> str | None:
    """Why these anchors do not make a `type_`, or None when they do."""
    need = TYPES[type_]
    if len(points) != need:
        return f"a {type_} needs {need} point{'s' if need > 1 else ''}"
    clock = clock or float

    def no_time(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return clock(a["t"]) == clock(b["t"])

    if type_ in ("trendline", "ray", "extended"):
        if no_time(points[0], points[1]):
            return "the two points need trading time between them"
    elif type_ in ("rect", "fib"):
        if no_time(points[0], points[1]):
            return "the two corners need trading time between them"
        if points[0]["p"] == points[1]["p"]:
            return "the two corners need different prices"
    elif type_ == "measure":
        if no_time(points[0], points[1]) and points[0]["p"] == points[1]["p"]:
            return "the two points must differ"
    elif type_ == "channel":
        if no_time(points[0], points[1]):
            return "the base line needs trading time between its two points"
        if abs(channel_offset(points, clock)) < 1e-9:
            return "the third point must not be on the base line"
    elif type_ == "text" and not (label or "").strip():
        return "a text needs its text"
    return None
