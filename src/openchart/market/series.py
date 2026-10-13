"""Historical candle series for the API.

- Sub-minute bars: from stored ticks.
- 1m to 30m: from stored M1.
- 1h: from H1 where M1 does not reach, M1 from there.
- 4h, D, W: complete bars from the cache (`market.cache`), the forming bar computed
  from base data on the fly.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from openchart.market.aggregate import PRICES, empty_frame, resample, ticks_frame
from openchart.market.cache import CandleCache, is_cached
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import Kind, Timeframe
from openchart.providers.base import MID_DECIMALS, Tick
from openchart.store.candles import CandleStore
from openchart.store.ticks import read_ticks

MAX_LIMIT = 5000
DEFAULT_LIMIT = 1000
MAX_TICK_DAYS = 3  # sub-minute history is read from tick files; keep requests bounded


def _days(start: int, end: int) -> list[str]:
    first = datetime.fromtimestamp(start, UTC).date()
    last = datetime.fromtimestamp(max(start, end - 1), UTC).date()
    return [str(first + timedelta(days=i)) for i in range((last - first).days + 1)]


class SeriesReader:
    def __init__(
        self,
        store: CandleStore,
        ticks_root: str | Path | None = None,
        buffered_ticks=None,  # callable(instrument) -> list[Tick] not yet written to disk
        cache: CandleCache | None = None,
    ):
        self.store = store
        self.cache = cache
        self.ticks_root = Path(ticks_root) if ticks_root else None
        self.buffered_ticks = buffered_ticks

    async def candles(
        self,
        source: str,
        instrument: str,
        tf: Timeframe,
        *,
        price: str = "mid",
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
        now: float | None = None,
    ) -> pd.DataFrame:
        """Bars with `start <= ts < end`.

        With `start`, returns the first `limit` bars from there (page forward by asking
        again from the last `ts`). Without `start`, returns the latest `limit` bars.

        Adds a boolean `complete` column: False for a bar whose period is not over.
        """
        if price not in PRICES:
            raise ValueError(f"price must be one of {PRICES}")
        now = time.time() if now is None else now
        end = int(now) + 1 if end is None else end
        limit = min(limit or (DEFAULT_LIMIT if start is None else MAX_LIMIT), MAX_LIMIT)

        if tf.kind is Kind.TICK:
            bars = await self._from_ticks(source, instrument, tf, price, start, end, limit)
        elif self.cache is not None and is_cached(tf):
            bars = await self._cached(source, instrument, tf, price, start, end, limit)
        elif start is not None:
            bars = await self._from_base(source, instrument, tf, price, start, end)
        else:
            bars = await self._latest_from_base(source, instrument, tf, price, end, limit)
        bars = (bars.head(limit) if start is not None else bars.tail(limit)).reset_index(drop=True)
        if bars.empty:
            bars["complete"] = pd.Series(dtype=bool)
            return bars
        last_end = bar_bounds(int(bars["ts"].iloc[-1]), tf)[1]
        complete = np.ones(len(bars), dtype=bool)
        complete[-1] = last_end <= now
        bars["complete"] = complete
        return bars

    @staticmethod
    def _uses_h1(tf: Timeframe) -> bool:
        return tf.kind is not Kind.TICK and tf.seconds >= 3600

    async def _from_base(self, source, instrument, tf, price, start, end) -> pd.DataFrame:
        # Bars are never cut: one that starts before `start` is left out, and one that
        # starts before `end` is returned whole.
        lo, hi = bar_bounds(start, tf)
        read_end = bar_bounds(end - 1, tf)[1]
        frame = await self.store.read_base_frame(
            source, instrument, lo if lo == start else hi, read_end, price, self._uses_h1(tf)
        )
        bars = resample(frame, tf)
        return bars[bars["ts"] < end].reset_index(drop=True)

    async def _latest_from_base(self, source, instrument, tf, price, end, limit) -> pd.DataFrame:
        first = await self.store.first_base_ts(source, instrument, self._uses_h1(tf))
        if first is None:
            return empty_frame()
        # Calendar span for `limit` bars, padded for weekends; doubled until enough.
        span = int(limit * tf.seconds * 1.5) + 3 * 86400
        while True:
            start = max(first, end - span)
            bars = await self._from_base(
                source, instrument, tf, price, bar_bounds(start, tf)[0], end
            )
            if len(bars) >= limit or start <= first:
                return bars
            span *= 2

    async def _cached(self, source, instrument, tf, price, start, end, limit) -> pd.DataFrame:
        """Cached complete bars, plus bars after the cache computed from base data."""
        assert self.cache is not None
        await self.cache.extend(source, instrument, tf)
        last = await self.store.cache_last_ts(source, instrument, tf.name)
        cached_until = bar_bounds(last, tf)[1] if last is not None else None
        if cached_until is None:  # nothing cached: no base data, or none complete yet
            if start is not None:
                return await self._from_base(source, instrument, tf, price, start, end)
            return await self._latest_from_base(source, instrument, tf, price, end, limit)

        tail = empty_frame()
        if end > cached_until:
            tail = await self._from_base(
                source, instrument, tf, price, max(start or 0, cached_until), end
            )
        if start is not None:
            head = await self.store.read_cache(
                source, instrument, tf.name, start, min(end, cached_until), price, limit
            )
        else:
            want = max(0, limit - len(tail))
            head = await self.store.read_cache(
                source, instrument, tf.name, 0, min(end, cached_until), price, want, latest=True
            )
        parts = [p for p in (head, tail) if not p.empty]
        if not parts:
            return empty_frame()
        return pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    async def _from_ticks(self, source, instrument, tf, price, start, end, limit) -> pd.DataFrame:
        if start is None:
            start = end - limit * tf.seconds * 3
        start = max(start, end - MAX_TICK_DAYS * 86400)
        frames = []
        if self.ticks_root is not None:
            for day in _days(start, end):
                table = read_ticks(self.ticks_root, source, instrument, day)
                if table.num_rows:
                    ts = table.column("ts").cast("int64").to_numpy() / 1_000_000
                    bid = table.column("bid").to_numpy()
                    ask = table.column("ask").to_numpy()
                    frames.append((ts, bid, ask))
        if self.buffered_ticks is not None:
            buffered: list[Tick] = self.buffered_ticks(instrument)
            if buffered:
                frames.append(
                    (
                        np.array([t.ts for t in buffered]),
                        np.array([t.bid for t in buffered]),
                        np.array([t.ask for t in buffered]),
                    )
                )
        if not frames:
            return empty_frame()
        ts = np.concatenate([f[0] for f in frames])
        bid = np.concatenate([f[1] for f in frames])
        ask = np.concatenate([f[2] for f in frames])
        order = np.argsort(ts, kind="stable")
        ts, bid, ask = ts[order], bid[order], ask[order]
        keep = (ts >= start) & (ts < end)
        prices = {"bid": bid, "ask": ask, "mid": np.round((bid + ask) / 2, MID_DECIMALS)}[price]
        return resample(ticks_frame(ts[keep], prices[keep]), tf)
