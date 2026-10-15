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
    "atr",
    "barssince",
    "bb",
    "cci",
    "change",
    "cross",
    "crossover",
    "crossunder",
    "cum",
    "ema",
    "falling",
    "highest",
    "highestbars",
    "hma",
    "lowest",
    "lowestbars",
    "macd",
    "mom",
    "nz",
    "pivothigh",
    "pivotlow",
    "rising",
    "rma",
    "roc",
    "rsi",
    "shift",
    "sma",
    "stdev",
    "stoch",
    "sum",
    "tr",
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


def _lagged(x: np.ndarray, k: int) -> np.ndarray:
    """x[i - k] for each bar i; NaN for the first k bars."""
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def _weighted_sum(x: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """sum(weights[k] * x[i - k]) for k = 0 .. len(weights) - 1; NaN where any term is."""
    acc = np.zeros(len(x))
    for k, w in enumerate(weights):
        acc += w * _lagged(x, k)
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
    """True when each of the last `n` steps went up: `x[0] > x[1] > ... > x[n]` in Pine's
    indexing (`n` steps, `n + 1` values). `[2, 1, 3]` is not rising over 2 although 3 is
    above both earlier values. A gap anywhere in those `n + 1` values gives False.
    """
    s = _series(values)
    out = pd.Series(True, index=s.index)
    for k in range(_whole(n, "n", 1)):
        out &= s.shift(k) > s.shift(k + 1)
    return out.astype(bool)


def falling(values, n: int) -> pd.Series:
    """True when each of the last `n` steps went down: `x[0] < x[1] < ... < x[n]` in Pine's
    indexing (see `rising`)."""
    s = _series(values)
    out = pd.Series(True, index=s.index)
    for k in range(_whole(n, "n", 1)):
        out &= s.shift(k) < s.shift(k + 1)
    return out.astype(bool)


# Oscillators and volatility


def _aligned(*parts) -> list[pd.Series]:
    """Several series of the same length (high, low, close), all with the first one's index."""
    series = [_series(p) for p in parts]
    if len({len(s) for s in series}) > 1:
        raise ValueError(f"the series differ in length ({', '.join(str(len(s)) for s in series)})")
    for s in series[1:]:
        s.index = series[0].index
    return series


def _window_moments(x: np.ndarray, length: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per window of `length`: the mean, the population variance and the mean absolute
    deviation from the mean. NaN where the window has a gap or is not full yet."""
    mean = _weighted_sum(x, np.ones(length)) / length
    squares = np.zeros(len(x))
    absolutes = np.zeros(len(x))
    for k in range(length):
        d = _lagged(x, k) - mean
        squares += d * d
        absolutes += np.abs(d)
    return mean, squares / length, absolutes / length


def rsi(values, length: int) -> pd.Series:
    """Relative strength index: `100 - 100 / (1 + up / down)`, where `up` and `down` are the
    `rma` of the gains and of the losses of each bar's change. Between 0 and 100.

    A window with no loss at all gives 100 (also when nothing moved at all: 0 / 0), one with
    no gain gives 0. The first value is at bar `length` (it needs `length` changes).

    Warmup: `length` bars for the first value; the `rma` inside fades slowly, so about
    `20 * length` earlier bars agree with a longer history to 1e-8.
    """
    length = _length(length)
    s = _series(values)
    d = s.diff()
    up = rma(d.clip(lower=0), length).to_numpy()
    down = rma((-d).clip(lower=0), length).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(down == 0, 100.0, np.where(up == 0, 0.0, 100 - 100 / (1 + up / down)))
    out[np.isnan(up) | np.isnan(down)] = np.nan
    return pd.Series(out, index=s.index)


def tr(high, low, close) -> pd.Series:
    """True range: the greatest of `high - low`, `|high - previous close|` and
    `|low - previous close|`. The first bar, and a bar whose previous close is missing, has
    `high - low`. Warmup: none, valid from the first bar."""
    h, low_, c = _aligned(high, low, close)
    prev = c.shift(1)
    full = np.maximum(h - low_, np.maximum((h - prev).abs(), (low_ - prev).abs()))
    return pd.Series(np.where(prev.isna(), h - low_, full), index=h.index)


def atr(high, low, close, length: int) -> pd.Series:
    """Average true range: `rma(tr, length)`, seeded with the simple average of the first
    `length` true ranges, the first being `high - low`.

    Warmup: `length` bars for the first value (at bar `length - 1`); about `20 * length`
    earlier bars agree with a longer history to 1e-8.
    """
    return rma(tr(high, low, close), _length(length))


def stoch(close, high, low, length: int) -> pd.Series:
    """Stochastic %K, unsmoothed: `100 * (close - lowest low) / (highest high - lowest low)`
    over the last `length` bars, current bar included. Smooth it yourself with `sma`. A window
    with no range (high equals low throughout) is a gap.

    Warmup: `length` bars. Exact from the first value.
    """
    length = _length(length)
    c, h, low_ = _aligned(close, high, low)
    hh = h.rolling(length, min_periods=length).max()
    ll = low_.rolling(length, min_periods=length).min()
    span = hh - ll
    return (100 * (c - ll) / span).where(span > 0)


def cci(values, length: int) -> pd.Series:
    """Commodity channel index: `(x - sma) / (0.015 * mean absolute deviation)` over the last
    `length` values. A window with no deviation is a gap. Warmup: `length` bars."""
    length = _length(length)
    s = _series(values)
    x = s.to_numpy()
    mean, _, mad = _window_moments(x, length)
    # "No deviation" with a little room for the rounding of the mean (0.1 * 3 is not 0.3).
    flat = mad <= 1e-12 * np.maximum(1.0, np.abs(mean))
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(flat, np.nan, (x - mean) / (0.015 * mad))
    return pd.Series(out, index=s.index)


def stdev(values, length: int) -> pd.Series:
    """Standard deviation of the last `length` values, population form (divided by `length`,
    not `length - 1`), TradingView's default. Warmup: `length` bars."""
    length = _length(length)
    s = _series(values)
    _, variance, _ = _window_moments(s.to_numpy(), length)
    return pd.Series(np.sqrt(variance), index=s.index)


def bb(values, length: int, mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: `(basis, upper, lower)` with the `sma` as basis and `mult`
    population standard deviations above and below it. Warmup: `length` bars."""
    if isinstance(mult, bool) or not isinstance(mult, int | float) or not math.isfinite(mult):
        raise ValueError(f"mult must be a number, not {mult!r}")
    basis = sma(values, length)
    spread = mult * stdev(values, length)
    return basis, basis + spread, basis - spread


def macd(
    values, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD: `(macd, signal, histogram)`: `macd = ema(x, fast) - ema(x, slow)`,
    `signal = ema(macd, signal)` and `histogram = macd - signal`. The defaults are
    TradingView's 12, 26 and 9.

    The MACD line starts at bar `max(fast, slow) - 1`, the signal `signal - 1` bars later.
    Warmup: about `10 * (slow + signal)` earlier bars agree with a longer history to 1e-8.
    """
    fast = _whole(fast, "fast length", 1)
    slow = _whole(slow, "slow length", 1)
    signal = _whole(signal, "signal length", 1)
    line = ema(values, fast) - ema(values, slow)
    smoothed = ema(line, signal)
    return line, smoothed, line - smoothed


# Windows and pivots


def highest(values, length: int) -> pd.Series:
    """The highest value of the last `length` bars, the current bar included. A gap in the
    window makes the value a gap. Warmup: `length` bars. Exact from the first value."""
    length = _length(length)
    return _series(values).rolling(length, min_periods=length).max()


def lowest(values, length: int) -> pd.Series:
    """The lowest value of the last `length` bars, the current bar included (see `highest`)."""
    length = _length(length)
    return _series(values).rolling(length, min_periods=length).min()


def _extreme_offset(values, length: int, better) -> pd.Series:
    s = _series(values)
    x = s.to_numpy()
    best = x.copy()
    back = np.zeros(len(x))
    bad = np.isnan(x)
    for k in range(1, length):
        candidate = _lagged(x, k)
        bad |= np.isnan(candidate)
        with np.errstate(invalid="ignore"):
            take = better(candidate, best)  # strictly: on a tie the more recent bar stays
        best = np.where(take, candidate, best)
        back = np.where(take, k, back)
    out = -back
    out[bad] = np.nan
    return pd.Series(out, index=s.index)


def highestbars(values, length: int) -> pd.Series:
    """Where the highest value of the last `length` bars is, as Pine's `ta.highestbars`: 0 when
    it is the current bar, -1 for the bar before, down to `-(length - 1)`. When the highest
    value occurs more than once, the most recent bar. A gap in the window gives a gap.

    Warmup: `length` bars. Exact from the first value.
    """
    return _extreme_offset(values, _length(length), np.greater)


def lowestbars(values, length: int) -> pd.Series:
    """Where the lowest value of the last `length` bars is (see `highestbars`; ties also go
    to the most recent bar)."""
    return _extreme_offset(values, _length(length), np.less)


def sum(values, length: int) -> pd.Series:
    """The sum of the last `length` values. A gap in the window gives a gap.

    Warmup: `length` bars. Exact from the first value.
    """
    length = _length(length)
    s = _series(values)
    return pd.Series(_weighted_sum(s.to_numpy(), np.ones(length)), index=s.index)


def cum(values) -> pd.Series:
    """The running total from the first bar. A gap is a gap on its own bar and the total
    carries on after it.

    Warmup: unlike every other helper, no number of earlier bars makes it agree with a longer
    history, since the total includes all of it. Use `sum` over a window, or a difference.
    """
    s = _series(values)
    return pd.Series(np.cumsum(np.nan_to_num(s.to_numpy())), index=s.index).where(s.notna())


# Which ties make a bar a pivot. UNCONFIRMED against TradingView (docs/decisions.md): to
# change the rule, change this one line. The pair is (left side strict, right side strict);
# strict means a bar equal to the pivot on that side stops it being a pivot.
_PIVOT_STRICT = (True, True)


def _pivot(values, left: int, right: int, above: bool) -> pd.Series:
    left = _whole(left, "left", 1)
    right = _whole(right, "right", 1)
    s = _series(values)
    x = s.to_numpy()
    centre = _lagged(x, right)  # the candidate for the bar `right` bars back
    strict_left, strict_right = _PIVOT_STRICT
    found = ~np.isnan(centre)
    with np.errstate(invalid="ignore"):  # a gap compares False, so there is no pivot
        for k in range(left + right + 1):
            if k == right:
                continue
            other = _lagged(x, k)
            strict = strict_right if k < right else strict_left
            if above:
                found &= (centre > other) if strict else (centre >= other)
            else:
                found &= (centre < other) if strict else (centre <= other)
    return pd.Series(np.where(found, centre, np.nan), index=s.index)


def pivothigh(values, left: int, right: int) -> pd.Series:
    """Pivot highs: a bar higher than the `left` bars before it and the `right` bars after it.

    The pivot's price is reported on the bar where it is confirmed, `right` bars after the
    pivot itself, and everywhere else the value is a gap. So the chart shows a pivot `right`
    bars late, and never before it can be known. A gap among those bars means no pivot. How
    equal values count: `_PIVOT_STRICT` (equal prices on either side stop a pivot).

    Warmup: `left + right + 1` bars for the first possible pivot.
    """
    return _pivot(values, left, right, True)


def pivotlow(values, left: int, right: int) -> pd.Series:
    """Pivot lows: the mirror image of `pivothigh`."""
    return _pivot(values, left, right, False)


def mom(values, length: int) -> pd.Series:
    """Momentum: `x - x[length]`. Warmup: first value at bar `length`."""
    s = _series(values)
    return s - s.shift(_length(length))


def roc(values, length: int) -> pd.Series:
    """Rate of change in percent: `100 * (x - x[length]) / x[length]`. A change from zero is
    a gap. Warmup: first value at bar `length`."""
    s = _series(values)
    before = s.shift(_length(length))
    return (100 * (s - before) / before).where(before != 0)
