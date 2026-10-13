"""Building blocks for indicators. They take and return series, so they compose: an
indicator can smooth another one's output (`ema(rsi(...), 9)`)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(values: pd.Series, length: int) -> pd.Series:
    """Simple moving average; NaN for the first `length - 1` values."""
    values = pd.Series(values, dtype="float64")
    return values.rolling(length, min_periods=length).mean()


def ema(values: pd.Series, length: int) -> pd.Series:
    """Exponential moving average with alpha = 2 / (length + 1).

    Seeded like TradingView's `ta.ema`: the first value (at the `length`-th bar) is the
    simple average of the first `length` values, then
    ema[i] = alpha * value[i] + (1 - alpha) * ema[i - 1]. Earlier values are NaN. Leading
    NaN in the input (another indicator's warm-up) are skipped before seeding.
    """
    values = pd.Series(values, dtype="float64").reset_index(drop=True)
    out = pd.Series(np.nan, index=values.index)
    valid = values.notna().to_numpy()
    if not valid.any():
        return out
    start = int(np.argmax(valid))
    if len(values) - start < length:
        return out
    seed_at = start + length - 1
    tail = values.iloc[seed_at:].copy()
    tail.iloc[0] = values.iloc[start : seed_at + 1].mean()
    # pandas' recursive form (adjust=False): y0 = x0, y[i] = (1 - a) * y[i-1] + a * x[i]
    out.iloc[seed_at:] = tail.ewm(alpha=2 / (length + 1), adjust=False).mean().to_numpy()
    return out
