"""Line geometry (SPEC section 9), shared by drawings and, from M6, the alert engine.

A line is two anchors (t, p): t in UTC epoch seconds, p a price. Its slope is measured
on the instrument's trading clock (`market.sessions.TradingClock`): time only counts
while the market is open, so the line keeps its slope across weekends and breaks and
is straight on the chart. The frontend (`frontend/src/drawings/geometry.ts`) uses the
same formula.

    line_price(t) = p1 + (p2 - p1) * (T(t) - T(t1)) / (T(t2) - T(t1))

- trendline: only for t1 <= t <= t2
- ray: from t1 towards point 2, to infinity
- extended: everywhere
- hline: p1 everywhere
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

INF = float("inf")

Clock = Callable[[float], float]


def _clock_time(t: float) -> float:
    return float(t)


def time_range(kind: str, points: Sequence[dict[str, Any]]) -> tuple[float, float]:
    if kind in ("hline", "extended"):
        return (-INF, INF)
    a, b = points[0]["t"], points[1]["t"]
    if kind == "trendline":
        return (min(a, b), max(a, b))
    if kind == "ray":
        return (a, INF) if b >= a else (-INF, a)
    raise ValueError(f"unknown drawing type {kind!r}")


def line_price(
    kind: str, points: Sequence[dict[str, Any]], t: float, clock: Clock = _clock_time
) -> float | None:
    """Price of the line at time `t`, or None where the drawing does not exist."""
    if kind == "hline":
        return float(points[0]["p"])
    lo, hi = time_range(kind, points)
    if not lo <= t <= hi:
        return None
    (t1, p1), (t2, p2) = ((pt["t"], pt["p"]) for pt in points)
    span = clock(t2) - clock(t1)
    if span == 0:
        return None
    return p1 + (p2 - p1) * (clock(t) - clock(t1)) / span
