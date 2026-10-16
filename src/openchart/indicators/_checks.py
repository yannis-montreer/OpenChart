"""Save-time checks of a custom indicator (CI-2 step c): does it read later bars, and is its
declared warmup long enough? Warnings for the author, never a block.

The script runs only in the worker. The server sends it two shortened copies of the bars a
chart request already computed, and compares what comes back with that request's own
answer, here. A verdict computed by the worker would be one more thing from code the
server does not control (owner, CI-2 c: B).

- **Lookahead:** the same bars without the last K (50, fewer for short inputs). Both runs
  start on the same bar, so a script that only reads the past gives the same values on
  every shared bar, bit for bit; any shared value that differs means the script reads
  later bars, or does not give the same result twice (random numbers, the clock).
- **Warmup:** the same bars starting `start` bars later. Past the offset run's own declared
  warmup, its values must agree with the full run's: if they do not, the values depend on
  more history than declared. If they still differ on the last bar, they depend on all of
  it (`ta.cum`, running totals): no warmup can fix that.

It detects some lookahead, not all (docs/custom-indicators.md, tests/test_lookahead_checks.py
pins what it misses). A passing check does not make a script safe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

LOOKAHEAD_BARS = 50  # removed for the lookahead run; a quarter of the bars if fewer
LOOKAHEAD_TOLERANCE = 1e-9  # x the output's largest value: FFT and summation-order noise
WARMUP_TOLERANCE = 1e-7  # the helpers document 1e-8 agreement at their warmup
WARMUP_OFFSET = 50  # the warmup run starts max(warmup, 50) bars later...
MIN_COMPARED = 20  # ...leaving at least max(20, n // 4) bars to compare
MIN_BARS = 8  # fewer: no lookahead check
RUN_SHARE = 0.8  # a check run starts only if the last run took at most this share of the deadline


@dataclass(frozen=True)
class Plan:
    n: int
    warmup: int
    cut: int | None = None  # bars removed for the lookahead run; None: skipped
    start: int | None = None  # where the warmup run starts; None: skipped
    lookahead_skip: str | None = None
    warmup_skip: str | None = None


def plan(n: int, warmup: int) -> Plan:
    """Which runs a check makes on `n` bars with the declared `warmup`."""
    cut = start = None
    lookahead_skip = warmup_skip = None
    if n >= MIN_BARS:
        cut = min(LOOKAHEAD_BARS, n // 4)
    else:
        lookahead_skip = f"too few bars to check for lookahead ({n}; at least {MIN_BARS})"
    compared = max(MIN_COMPARED, n // 4)
    start = min(max(warmup, WARMUP_OFFSET), n - warmup - compared)
    if start < 1:
        start = None
        warmup_skip = (
            f"too few bars to check the warmup of {warmup} bars "
            f"({n}; at least {warmup + MIN_COMPARED + 1})"
        )
    return Plan(n, warmup, cut, start, lookahead_skip, warmup_skip)


def fits(seconds: float, deadline: float) -> bool:
    """May a run that is expected to take `seconds` start under this deadline?"""
    return seconds <= RUN_SHARE * deadline


def _gaps(a: np.ndarray) -> np.ndarray:
    return ~np.isfinite(a)


def _scale(values: np.ndarray) -> float:
    """The output's largest finite absolute value (0 when it has none: then exact)."""
    finite = values[~_gaps(values)]
    return float(np.max(np.abs(finite))) if finite.size else 0.0


def _differs(
    full: np.ndarray, other: np.ndarray, boolean: bool, tolerance: float, scale: float
) -> np.ndarray:
    """Where two runs disagree: a gap (NaN or an infinity) equals a gap, a gap differs from
    a number, true/false must be equal, numbers within `tolerance` x `scale` (the output's
    largest value in the whole full run, not only on the bars compared)."""
    full_gap, other_gap = _gaps(full), _gaps(other)
    if boolean:
        return full != other
    with np.errstate(invalid="ignore"):
        far = np.abs(full - other) > tolerance * scale
    return (full_gap != other_gap) | (~full_gap & ~other_gap & far)


def _value(a: float, boolean: bool) -> float | None:
    if not math.isfinite(a):
        return None
    return float(a)


def _shown(v: float | None, boolean: bool) -> str:
    if v is None:
        return "a gap"
    if boolean:
        return "true" if v else "false"
    return f"{v:.10g}"


def _finding(check: str, level: str, oid: str, t: float, full: Any, other: Any, message: str):
    return {
        "check": check,
        "level": level,
        "output": oid,
        "t": int(t) if float(t).is_integer() else float(t),
        "full": full,
        "other": other,
        "message": message,
    }


def lookahead_findings(indicator, full, part, t) -> list[dict[str, Any]]:
    """`full`: each value output on all n bars; `part`: on the first n - K. One warning
    per output that differs, at its first differing bar."""
    out = []
    for oid, o in indicator.data_outputs():
        boolean = o.data == "bool"
        a, b = np.asarray(full[oid], dtype="float64"), np.asarray(part[oid], dtype="float64")
        n, kept = len(a), len(b)
        bad = np.flatnonzero(_differs(a[:kept], b, boolean, LOOKAHEAD_TOLERANCE, _scale(a)))
        if not bad.size:
            continue
        i = int(bad[0])
        va, vb = _value(a[i], boolean), _value(b[i], boolean)
        out.append(
            _finding(
                "lookahead",
                "warning",
                oid,
                t[i],
                va,
                vb,
                f"'{oid}' changes when the last {n - kept} bars are removed: on this bar it "
                f"is {_shown(va, boolean)} with all {n} bars and {_shown(vb, boolean)} with "
                f"{kept}. The script reads later bars, or does not give the same result twice "
                "(random numbers, the clock).",
            )
        )
    return out


def warmup_findings(indicator, full, offset, plan: Plan, t) -> list[dict[str, Any]]:
    """`offset`: each value output on the bars from `plan.start`. Compared from the bar
    where the offset run has its declared warmup behind it. A true/false output that
    differs only where every line agrees is a note (a near-tie in rounding), not a
    warning, when the indicator has lines to tell by."""
    s, w = plan.start, plan.warmup
    first = s + w  # the first compared bar, in the full run
    lines_agree = True
    diffs = {}
    outputs = indicator.data_outputs()
    for oid, o in outputs:
        boolean = o.data == "bool"
        whole = np.asarray(full[oid], dtype="float64")
        a, b = whole[first:], np.asarray(offset[oid], dtype="float64")[w:]
        bad = np.flatnonzero(_differs(a, b, boolean, WARMUP_TOLERANCE, _scale(whole)))
        diffs[oid] = (bad, a, b, boolean)
        if bad.size and not boolean:
            lines_agree = False
    has_lines = any(o.data != "bool" for _, o in outputs)
    out = []
    for oid, (bad, a, b, boolean) in diffs.items():
        if not bad.size:
            continue
        i, j = int(bad[0]), int(bad[-1])
        va, vb = _value(a[i], boolean), _value(b[i], boolean)
        at = first + i  # the bar, in the full run
        before_full, before_offset = at, at - s
        values = (
            f"on this bar it is {_shown(va, boolean)} with {before_full} bars before it and "
            f"{_shown(vb, boolean)} with {before_offset}"
        )
        if boolean and has_lines and lines_agree:
            out.append(
                _finding(
                    "warmup",
                    "note",
                    oid,
                    t[at],
                    va,
                    vb,
                    f"'{oid}' differs on {bad.size} bar(s) where every line agrees within "
                    f"rounding ({values}): most likely a near-tie, two values equal to "
                    "within rounding, not a warmup that is too short.",
                )
            )
            continue
        if first + j == plan.n - 1:
            message = (
                f"'{oid}' never agrees with a shorter history: {values}, and it still differs "
                "on the last bar. It depends on every earlier bar (like ta.cum or a running "
                "total), so its values depend on where the chart's history starts; no warmup "
                "can fix that."
            )
            needed = None
        else:
            needed = j + w + 1  # the last differing bar had j + w bars before it
            message = (
                f"'{oid}' depends on more history than the declared warmup of {w} bars: "
                f"{values}. Declare a warmup of at least {needed} bars."
            )
        if boolean:
            message += (
                " If it compares two values that can be equal, this may also be a near-tie "
                "in rounding."
            )
        f = _finding("warmup", "warning", oid, t[at], va, vb, message)
        f["needed"] = needed
        out.append(f)
    return out


def note(check: str, message: str) -> dict[str, Any]:
    """Why a check did not run (or ended early): no output, no bar."""
    return {
        "check": check,
        "level": "note",
        "output": None,
        "t": None,
        "full": None,
        "other": None,
        "message": message,
    }


def state(findings: list[dict[str, Any]]) -> str:
    return "warned" if any(f["level"] == "warning" for f in findings) else "ok"
