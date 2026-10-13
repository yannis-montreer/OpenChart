"""Candle aggregation: M1 history to any timeframe, and ticks to bars.

The same rule everywhere: open = first open, high = max, low = min, close = last close,
volume = sum. Bar boundaries come from `market.sessions`.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from openchart.market.sessions import bar_starts
from openchart.market.timeframes import Timeframe
from openchart.providers.base import OHLC, Candle, Tick

PRICES = ("mid", "bid", "ask")
COLUMNS = ["ts", "o", "h", "l", "c", "v"]
# All three price series: ts, bid_o..bid_c, ask_o..ask_c, mid_o..mid_c, v.
FULL_COLUMNS = ["ts", *(f"{p}_{k}" for p in ("bid", "ask", "mid") for k in "ohlc"), "v"]


def empty_frame(columns: list[str] = COLUMNS) -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype="int64" if c in ("ts", "v") else "float64") for c in columns}
    )


def resample_full(frame: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """`resample` for a frame with FULL_COLUMNS (bid, ask and mid at once)."""
    if frame.empty:
        return empty_frame(FULL_COLUMNS)
    keys = bar_starts(frame["ts"].to_numpy(), tf)
    rules = {"o": "first", "h": "max", "l": "min", "c": "last"}
    agg = {f"{p}_{k}": rule for p in ("bid", "ask", "mid") for k, rule in rules.items()}
    agg["v"] = "sum"
    out = frame.groupby(keys, sort=True).agg(agg)
    out.insert(0, "ts", out.index.to_numpy(dtype=np.int64))
    return out.reset_index(drop=True)[FULL_COLUMNS]


def resample(frame: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """Aggregate a frame with columns ts, o, h, l, c, v (sorted by ts) into `tf` bars.

    The result has the same columns; `ts` is the bar start.
    """
    if frame.empty:
        return empty_frame()
    keys = bar_starts(frame["ts"].to_numpy(), tf)
    grouped = frame.groupby(keys, sort=True)
    out = pd.DataFrame(
        {
            "o": grouped["o"].first(),
            "h": grouped["h"].max(),
            "l": grouped["l"].min(),
            "c": grouped["c"].last(),
            "v": grouped["v"].sum(),
        }
    )
    out.insert(0, "ts", out.index.to_numpy(dtype=np.int64))
    return out.reset_index(drop=True)


def ticks_frame(ts: np.ndarray, prices: np.ndarray) -> pd.DataFrame:
    """One-tick 'bars' (o = h = l = c = price, v = 1) ready for `resample`."""
    ts = np.asarray(ts)
    prices = np.asarray(prices, dtype=np.float64)
    return pd.DataFrame(
        {
            "ts": np.floor(ts).astype(np.int64),
            "o": prices,
            "h": prices,
            "l": prices,
            "c": prices,
            "v": np.ones(len(prices), dtype=np.int64),
        }
    )


class Bar:
    """A mutable bar holding bid, ask and mid OHLC, used for live building."""

    __slots__ = ("ask", "bid", "mid", "ts", "volume")

    def __init__(self, ts: int, bid: list[float], ask: list[float], mid: list[float], volume: int):
        self.ts = ts
        self.bid, self.ask, self.mid = bid, ask, mid
        self.volume = volume

    @classmethod
    def from_tick(cls, ts: int, tick: Tick) -> Bar:
        m = tick.mid
        return cls(ts, [tick.bid] * 4, [tick.ask] * 4, [m] * 4, 1)

    @classmethod
    def from_candle(cls, c: Candle) -> Bar:
        return cls(c.ts, list(c.bid), list(c.ask), list(c.mid), c.volume)

    def copy(self) -> Bar:
        return Bar(self.ts, self.bid[:], self.ask[:], self.mid[:], self.volume)

    @staticmethod
    def _add(ohlc: list[float], price: float) -> None:
        if price > ohlc[1]:
            ohlc[1] = price
        if price < ohlc[2]:
            ohlc[2] = price
        ohlc[3] = price

    def add_tick(self, tick: Tick) -> None:
        self._add(self.bid, tick.bid)
        self._add(self.ask, tick.ask)
        self._add(self.mid, tick.mid)
        self.volume += 1

    def merged(self, later: Bar | None) -> Bar:
        """A new bar: this one followed by `later` (keeps this bar's ts and open)."""
        out = self.copy()
        if later is None:
            return out
        for mine, theirs in ((out.bid, later.bid), (out.ask, later.ask), (out.mid, later.mid)):
            mine[1] = max(mine[1], theirs[1])
            mine[2] = min(mine[2], theirs[2])
            mine[3] = theirs[3]
        out.volume += later.volume
        return out

    def with_ts(self, ts: int) -> Bar:
        out = self.copy()
        out.ts = ts
        return out

    def to_candle(self, complete: bool) -> Candle:
        return Candle(
            self.ts, OHLC(*self.bid), OHLC(*self.ask), OHLC(*self.mid), self.volume, complete
        )

    def series(self, price: str) -> list[float]:
        return getattr(self, price)


def merge_bars(bars: Iterable[Bar], ts: int) -> Bar | None:
    """Fold bars (in time order) into one bar starting at `ts`."""
    out: Bar | None = None
    for bar in bars:
        out = bar.with_ts(ts) if out is None else out.merged(bar)
    return out
