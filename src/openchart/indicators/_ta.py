"""The `ta` helper library: building blocks for indicators, `from openchart.indicators import ta`.

Every helper takes pandas Series (or lists) aligned to the bars and returns a Series with the
same index, so they compose: `ta.ema(ta.rma(x, 14), 9)`.

The definitions follow TradingView's documented ones, including how each starts, and are
written from the public formulas. Common rules:

- **No lookahead.** The value at a bar uses that bar and earlier ones only. `shift` refuses
  offsets below 1.
- **A gap (NaN) in the input** gives NaN wherever it is in the average's window. `ema` and
  `rma` give NaN at the gap and start again (seeded with the simple average) once `length`
  values in a row are present, as TradingView's documented recursion does.
- **Not enough bars** gives NaN, never an error. Booleans (`crossover`, `rising`, ...) are
  False wherever they cannot be told.
- **Warmup**, the earlier bars a helper needs for its values to be settled, is in each
  docstring. `warmup(params)` in an indicator file should cover the largest of them.
"""

from __future__ import annotations

import math
from numbers import Integral

import numpy as np
import pandas as pd

__all__ = [
    "barssince",
    "change",
    "cross",
    "crossover",
    "crossunder",
    "ema",
    "falling",
    "hma",
    "nz",
    "rising",
    "rma",
    "shift",
    "sma",
    "valuewhen",
    "vwma",
    "wma",
]


# Argument handling


def _series(x) -> pd.Series:
    return pd.Series(x, dtype="float64")


def _flags(x) -> pd.Series:
    """A condition as booleans; a gap counts as False."""
    s = pd.Series(x)
    if s.dtype != bool:
        s = s.fillna(False).astype(bool)
    return s


def _whole(value, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be a whole number of at least {minimum}, not {value!r}")
    return int(value)


def _length(length) -> int:
    return _whole(length, "length", 1)


def _pair(a, b) -> tuple[pd.Series, pd.Series]:
    """Two operands that may each be a series or a plain number (`ta.crossover(rsi, 30)`)."""
    a_is_series = isinstance(a, pd.Series | list | tuple | np.ndarray)
    b_is_series = isinstance(b, pd.Series | list | tuple | np.ndarray)
    if not a_is_series and not b_is_series:
        raise ValueError("at least one of the two values must be a series")
    if a_is_series:
        a_s = _series(a)
        b_s = _series(b) if b_is_series else pd.Series(float(b), index=a_s.index)
    else:
        b_s = _series(b)
        a_s = pd.Series(float(a), index=b_s.index)
    if len(a_s) != len(b_s):
        raise ValueError(f"the two series differ in length ({len(a_s)} and {len(b_s)})")
    b_s.index = a_s.index
    return a_s, b_s


# Averages


def sma(values, length: int) -> pd.Series:
    """Simple moving average of the last `length` values; NaN for the first `length - 1`.

    Warmup: `length` bars. Exact from the first value.
    """
    length = _length(length)
    return _series(values).rolling(length, min_periods=length).mean()


def _recursive(values, length: int, alpha: float) -> pd.Series:
    """The recursion behind `ema` and `rma`, as TradingView documents it:
    `sum := na(sum[1]) ? sma(src, length) : alpha * src + (1 - alpha) * sum[1]`.

    In every run of `length` or more values in a row, the first value is the simple average
    of the first `length` of them and the recursion goes on from there; a gap ends the run
    (the output is NaN there) and a new run seeds again.
    """
    s = _series(values)
    x = s.to_numpy()
    out = np.full(len(x), np.nan)
    present = (~np.isnan(x)).astype(np.int8)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], present, [0]))))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        run = x[start:stop]
        if len(run) < length:
            continue
        tail = run[length - 1 :].copy()
        tail[0] = run[:length].mean()
        out[start + length - 1 : stop] = (
            pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        )
    return pd.Series(out, index=s.index)


def ema(values, length: int) -> pd.Series:
    """Exponential moving average, alpha = 2 / (length + 1), seeded like TradingView's
    `ta.ema`: the first value (at the `length`-th bar) is the simple average of the first
    `length` values. Leading NaN in the input (another indicator's warmup) are skipped.

    Warmup: the seed leaves a trace that fades as (1 - alpha) ** bars. About 10 * length
    earlier bars agree with a longer history to 1e-8; `length` bars give the first value.
    """
    return _recursive(values, _length(length), 2 / (_length(length) + 1))


def rma(values, length: int) -> pd.Series:
    """Wilder's moving average (`ta.rma`), alpha = 1 / length, seeded with the simple average
    of the first `length` values like `ema`. It is the smoothing inside `rsi` and `atr`.

    Warmup: it fades as (1 - 1/length) ** bars, slowly. About 20 * length earlier bars agree
    with a longer history to 1e-8 (`length` bars give the first value).
    """
    return _recursive(values, _length(length), 1 / _length(length))


def _weighted_sum(x: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """sum(weights[k] * x[i - k]) for k = 0 .. len(weights) - 1; NaN where any term is."""
    n = len(x)
    acc = np.zeros(n)
    for k, w in enumerate(weights):
        shifted = np.full(n, np.nan)
        if k < n:
            shifted[k:] = x[: n - k]
        acc += w * shifted
    return acc


def wma(values, length: int) -> pd.Series:
    """Weighted moving average: the newest of the last `length` values weighs `length`, the
    one before `length - 1`, down to 1, divided by their sum.

    Warmup: `length` bars. Exact from the first value.
    """
    length = _length(length)
    s = _series(values)
    weights = np.arange(length, 0, -1, dtype="float64")  # k = 0 (newest) weighs `length`
    out = _weighted_sum(s.to_numpy(), weights) / weights.sum()
    return pd.Series(out, index=s.index)


def vwma(values, volume, length: int) -> pd.Series:
    """Volume-weighted moving average: sum(value * volume) / sum(volume) over the last
    `length` bars. Volume is OANDA's tick count. A window with no volume at all is NaN.

    Warmup: `length` bars. Exact from the first value.
    """
    length = _length(length)
    s, v = _pair(values, volume)
    ones = np.ones(length)
    num = _weighted_sum((s * v).to_numpy(), ones)
    den = _weighted_sum(v.to_numpy(), ones)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den == 0, np.nan, num / den)
    return pd.Series(out, index=s.index)


def hma(values, length: int) -> pd.Series:
    """Hull moving average: `wma(2 * wma(x, length // 2) - wma(x, length), round(sqrt(length)))`.
    The half length is rounded down, the root to the nearest whole number, both at least 1.

    Warmup: first value at bar `length + round(sqrt(length)) - 1`; exact from there.
    """
    length = _length(length)
    half = max(1, length // 2)
    root = max(1, math.floor(math.sqrt(length) + 0.5))
    return wma(2 * wma(values, half) - wma(values, length), root)


# Series helpers


def shift(values, n: int) -> pd.Series:
    """The value `n` bars ago (Pine's `x[n]`); NaN for the first `n` bars.

    `n` must be 1 or more: an offset of 0 or below would read the current or future bars,
    which a chart cannot know when the bar forms.
    """
    if isinstance(n, bool) or not isinstance(n, Integral) or n < 1:
        raise ValueError(
            f"shift needs n >= 1 (a whole number of bars back), not {n!r}: "
            "a zero or negative offset would read the future"
        )
    return _series(values).shift(int(n))


def change(values, n: int = 1) -> pd.Series:
    """The difference to `n` bars ago: `x - shift(x, n)`. NaN for the first `n` bars."""
    s = _series(values)
    return s - shift(s, n)


def nz(values, replacement: float = 0.0) -> pd.Series:
    """Gaps replaced by `replacement` (0 by default)."""
    return _series(values).fillna(replacement)


def crossover(a, b) -> pd.Series:
    """True on the bar where `a` has moved above `b`: `a > b` now and `a <= b` on the bar
    before. Either may be a plain number (`crossover(rsi, 30)`). A gap on either bar is
    never a cross, and neither is the first bar.

    Warmup: one bar more than the inputs need.
    """
    a_s, b_s = _pair(a, b)
    return ((a_s > b_s) & (a_s.shift(1) <= b_s.shift(1))).astype(bool)


def crossunder(a, b) -> pd.Series:
    """True on the bar where `a` has moved below `b`: `a < b` now and `a >= b` on the bar
    before. Either may be a plain number."""
    a_s, b_s = _pair(a, b)
    return ((a_s < b_s) & (a_s.shift(1) >= b_s.shift(1))).astype(bool)


def cross(a, b) -> pd.Series:
    """True on a bar where `a` crossed `b` in either direction."""
    return crossover(a, b) | crossunder(a, b)


def barssince(condition) -> pd.Series:
    """Bars since `condition` was last True: 0 on a bar where it is True, 1 on the next, and
    so on. NaN before the first True. A gap in the condition counts as False."""
    c = _flags(condition)
    n = len(c)
    idx = np.arange(n)
    last = np.maximum.accumulate(np.where(c.to_numpy(), idx, -1)) if n else idx
    out = np.where(last >= 0, idx - last, np.nan)
    return pd.Series(out, index=c.index, dtype="float64")


def valuewhen(condition, values, n: int = 0) -> pd.Series:
    """The value of `values` on the bar where `condition` was True the `n`-th time back:
    0 is the latest such bar (this one, if it is True), 1 the one before, and so on. NaN
    until the condition has been True `n + 1` times."""
    n = _whole(n, "n", 0)
    c = _flags(condition)
    x = _series(values)
    if len(c) != len(x):
        raise ValueError(f"condition and values differ in length ({len(c)} and {len(x)})")
    flags = c.to_numpy()
    where = np.flatnonzero(flags)
    pick = np.cumsum(flags) - 1 - n  # position in `where` of the wanted True, per bar
    if len(where) == 0:
        return pd.Series(np.nan, index=x.index, dtype="float64")
    out = np.where(pick >= 0, x.to_numpy()[where[np.clip(pick, 0, None)]], np.nan)
    return pd.Series(out, index=x.index, dtype="float64")


def rising(values, n: int) -> pd.Series:
    """True when the value is higher than each of the previous `n` values. A gap anywhere
    in those `n + 1` values gives False.

    Note: this is the reading "greater than any previous source for length bars back" of
    the Pine reference, so `[1, 3, 2, 4]` is rising over 2 bars at the last bar although
    3 -> 2 went down. See docs/decisions.md.
    """
    n = _whole(n, "n", 1)
    s = _series(values)
    return (s > s.rolling(n, min_periods=n).max().shift(1)).astype(bool)


def falling(values, n: int) -> pd.Series:
    """True when the value is lower than each of the previous `n` values (see `rising`)."""
    n = _whole(n, "n", 1)
    s = _series(values)
    return (s < s.rolling(n, min_periods=n).min().shift(1)).astype(bool)
