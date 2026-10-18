"""Candle and instrument storage.

Two provider granularities are stored: M1 (the source of truth for recent history) and
H1 (deep history, where M1 does not reach). Complete 4h, D and W bars derived from
them are cached in `candles_cache`; every M1 or H1 write deletes the cached bars it
could affect, so the cache never serves stale data and can always be rebuilt.

Storage format (migration 0007): each (source, instrument) is a row of `series` with a
small integer id and a price `scale`; candle rows hold the id and the prices as
integers, `price * scale`. Reading divides by the scale in SQL, which gives back
exactly the double that was written, as long as the price has at most
`log10(scale)` decimals: `scale` is `10^(decimals + 1)`, one digit more than the
instrument's display precision, which OANDA's mid prices need. A price with more
digits is rounded to the scale and logged. Callers only ever see floats.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite
import numpy as np
import pandas as pd

from openchart.market.aggregate import COLUMNS, FULL_COLUMNS, PRICES, empty_frame
from openchart.market.sessions import TradingHours, bar_bounds, bar_start
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import OHLC, Candle, InstrumentInfo

log = logging.getLogger(__name__)

TABLES = {"M1": "candles_m1", "H1": "candles_h1"}
STEP_SECONDS = {"M1": 60, "H1": 3600}
CACHED_TIMEFRAMES = ("4h", "1D", "1W")
# Scale of a series whose instrument has no metadata (tests, tools): six decimals.
DEFAULT_SCALE = 10**6

_PRICE_NAMES = [f"{side}_{x}" for side in ("bid", "ask", "mid") for x in "ohlc"]
_PRICE_COLUMNS = ", ".join(_PRICE_NAMES)


def price_scale(decimals: int | None) -> int:
    """The integer factor prices of an instrument with these display decimals are stored in."""
    return DEFAULT_SCALE if decimals is None else 10 ** (decimals + 1)


def _decoded(names: list[str], scale: int) -> str:
    """SQL select list turning stored integers back into prices."""
    return ", ".join(f"{name} / {scale}.0" for name in names)


@dataclass(frozen=True)
class Series:
    id: int
    scale: int


def _upsert_sql(table: str, key: str = "series, ts") -> str:
    cols = [c.strip() for c in _PRICE_COLUMNS.split(",")] + ["volume"]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
    n = len(key.split(",")) + len(cols)
    return (
        f"INSERT INTO {table} ({key}, {_PRICE_COLUMNS}, volume)"
        f" VALUES ({', '.join('?' * n)})"
        f" ON CONFLICT ({key}) DO UPDATE SET {updates}"
    )


@dataclass(frozen=True)
class InstrumentRow:
    source: str
    instrument: str
    provider_symbol: str
    display_name: str
    decimals: int
    pip_size: float
    enabled: bool
    hours: TradingHours | None
    m1_depth: str | None = None  # minute history kept ('3m', '1y', '2y'); None: the default


WriteListener = Callable[[str, str, int | None, int], None]


class CandleStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        # The version of a series' candles: bumped on every M1/H1 write before it is queued
        # and again once it is committed (or failed), by `_writing`. The 4h/D/W cache and
        # the bar cache compare it to detect that data they read changed underneath them.
        self._epoch: dict[tuple[str, str], int] = defaultdict(int)
        self._series: dict[tuple[str, str], Series] = {}
        self._listeners: list[WriteListener] = []

    def epoch(self, source: str, instrument: str) -> int:
        return self._epoch[(source, instrument)]

    def on_write(self, listener: WriteListener) -> None:
        """Call `listener(source, instrument, oldest, epoch)` on every bump: `oldest` is the
        oldest time the write touches (None: any, e.g. a purge), `epoch` the new value."""
        self._listeners.append(listener)

    def _bump(self, source: str, instrument: str, oldest: int | None) -> None:
        key = (source, instrument)
        self._epoch[key] += 1
        for listener in self._listeners:
            listener(source, instrument, oldest, self._epoch[key])

    @asynccontextmanager
    async def _writing(
        self, source: str, instrument: str, oldest: int | None
    ) -> AsyncIterator[None]:
        """Every write of candle rows runs inside this: the epoch moves before the first
        statement is queued (a read under way will not be kept) and after the commit or
        failure (a read that saw the rows half written will not be kept either)."""
        self._bump(source, instrument, oldest)
        try:
            yield
        finally:
            self._bump(source, instrument, oldest)

    # Series (one per source and instrument)

    async def series(self, source: str, instrument: str, *, create: bool = False) -> Series | None:
        """The series of these candles. With `create`, made on first use (not committed:
        the write that needs it commits it), its scale from the instrument's decimals."""
        key = (source, instrument)
        if key in self._series:
            return self._series[key]
        sql = "SELECT id, scale FROM series WHERE source=? AND instrument=?"
        async with self.conn.execute(sql, key) as cur:
            row = await cur.fetchone()
        if row is None:
            if not create:
                return None
            decimals = await self._scalar(
                "SELECT decimals FROM instruments WHERE source=? AND instrument=?", *key
            )
            await self.conn.execute(
                "INSERT INTO series (source, instrument, scale) VALUES (?, ?, ?)",
                (*key, price_scale(decimals)),
            )
            async with self.conn.execute(sql, key) as cur:
                row = await cur.fetchone()
        self._series[key] = Series(row[0], row[1])
        return self._series[key]

    def _check_exact(self, source: str, instrument: str, scale: int, inexact: int) -> None:
        if inexact:
            log.warning(
                "%s/%s: %d candle(s) had prices with more decimals than the series stores"
                " (scale %d); they were rounded",
                source,
                instrument,
                inexact,
                scale,
            )

    # Writes

    async def upsert(
        self, source: str, instrument: str, candles: Iterable[Candle], granularity: str = "M1"
    ) -> int:
        """Insert or replace complete candles in one transaction. Returns rows written."""
        complete = [c for c in candles if c.complete]
        if not complete:
            return 0
        series = await self.series(source, instrument, create=True)
        assert series is not None
        scale = series.scale
        rows = []
        inexact = 0
        for c in complete:
            prices = (*c.bid, *c.ask, *c.mid)
            ints = [round(price * scale) for price in prices]
            if any(i / scale != price for i, price in zip(ints, prices, strict=True)):
                inexact += 1
            rows.append((series.id, c.ts, *ints, c.volume))
        self._check_exact(source, instrument, scale, inexact)
        stamps = [r[1] for r in rows]
        async with self._writing(source, instrument, min(stamps)):
            await self.conn.executemany(_upsert_sql(TABLES[granularity]), rows)
            await self._invalidate_cache(source, instrument, min(stamps), max(stamps))
            await self.conn.commit()
        return len(rows)

    async def upsert_m1(self, source: str, instrument: str, candles: Iterable[Candle]) -> int:
        return await self.upsert(source, instrument, candles, "M1")

    async def _invalidate_cache(self, source: str, instrument: str, lo: int, hi: int) -> None:
        """Drop the cached bars that rows written in `[lo, hi]` affect.

        That is every bar overlapping `[lo, hi]` plus the next hour, because writing M1
        can move the hour where M1 takes over from H1 (`h1_boundary`). At the end of the
        cache the bars are simply dropped (the cache grows forward again); anywhere else,
        before or inside it (a download extending history backward), the dropped range is
        recorded in `cache_dirty` and `CandleCache.extend` recomputes it.
        """
        series = await self.series(source, instrument)
        assert series is not None  # the write that calls this created it
        for name in CACHED_TIMEFRAMES:
            tf = TIMEFRAMES[name]
            start, end = bar_start(lo, tf), bar_bounds(hi + 3599, tf)[1]
            last = await self.cache_last_ts(source, instrument, name)
            if last is None:
                continue  # nothing cached: the cache starts from the oldest data anyway
            if end > last:
                await self.conn.execute(
                    "DELETE FROM candles_cache WHERE series=? AND timeframe=? AND ts>=?",
                    (series.id, name, start),
                )
                continue
            await self.conn.execute(
                "DELETE FROM candles_cache WHERE series=? AND timeframe=? AND ts>=? AND ts<?",
                (series.id, name, start, end),
            )
            await self.conn.execute(
                "INSERT INTO cache_dirty (source, instrument, timeframe, start_ts, end_ts)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, instrument, timeframe, start_ts)"
                " DO UPDATE SET end_ts=MAX(end_ts, excluded.end_ts)",
                (source, instrument, name, start, end),
            )

    async def cache_dirty(
        self, source: str, instrument: str, timeframe: str
    ) -> list[tuple[int, int]]:
        """Ranges `[start, end)` of cached bars waiting to be recomputed."""
        async with self.conn.execute(
            "SELECT start_ts, end_ts FROM cache_dirty WHERE source=? AND instrument=?"
            " AND timeframe=? ORDER BY start_ts",
            (source, instrument, timeframe),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    # Reads

    async def _scalar(self, sql: str, *params: object) -> int | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def count(self, source: str, instrument: str, granularity: str = "M1") -> int:
        series = await self.series(source, instrument)
        if series is None:
            return 0
        n = await self._scalar(
            f"SELECT COUNT(*) FROM {TABLES[granularity]} WHERE series=?", series.id
        )
        return n or 0

    async def first_ts(self, source: str, instrument: str, granularity: str = "M1") -> int | None:
        series = await self.series(source, instrument)
        if series is None:
            return None
        return await self._scalar(
            f"SELECT MIN(ts) FROM {TABLES[granularity]} WHERE series=?", series.id
        )

    async def last_ts(
        self, source: str, instrument: str, granularity: str = "M1", *, before: int | None = None
    ) -> int | None:
        """Newest stored candle, or the newest one strictly before `before`."""
        series = await self.series(source, instrument)
        if series is None:
            return None
        return await self._scalar(
            f"SELECT MAX(ts) FROM {TABLES[granularity]} WHERE series=? AND ts<?",
            series.id,
            2**62 if before is None else before,
        )

    async def timestamps(
        self,
        source: str,
        instrument: str,
        start: int | None = None,
        end: int | None = None,
        granularity: str = "M1",
    ) -> list[int]:
        series = await self.series(source, instrument)
        if series is None:
            return []
        sql = f"SELECT ts FROM {TABLES[granularity]} WHERE series=? AND ts>=? AND ts<? ORDER BY ts"
        params = (series.id, start or 0, end or 2**62)
        async with self.conn.execute(sql, params) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def read(
        self, source: str, instrument: str, start: int, end: int, granularity: str = "M1"
    ) -> list[Candle]:
        series = await self.series(source, instrument)
        if series is None:
            return []
        sql = (
            f"SELECT ts, {_decoded(_PRICE_NAMES, series.scale)}, volume FROM {TABLES[granularity]}"
            " WHERE series=? AND ts>=? AND ts<? ORDER BY ts"
        )
        async with self.conn.execute(sql, (series.id, start, end)) as cur:
            rows = await cur.fetchall()
        return [Candle(r[0], OHLC(*r[1:5]), OHLC(*r[5:9]), OHLC(*r[9:13]), r[13]) for r in rows]

    async def read_m1(self, source: str, instrument: str, start: int, end: int) -> list[Candle]:
        return await self.read(source, instrument, start, end, "M1")

    async def _frame(self, sql: str, params: tuple, columns: list[str]) -> pd.DataFrame:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        if not rows:
            return empty_frame(columns)
        frame = pd.DataFrame.from_records(rows, columns=columns)
        return frame.astype({"ts": "int64", "v": "int64"})

    async def read_frame(
        self,
        source: str,
        instrument: str,
        start: int,
        end: int,
        price: str | None = "mid",
        granularity: str = "M1",
    ) -> pd.DataFrame:
        """Rows in `[start, end)`. With `price`: columns ts, o, h, l, c, v for that price.
        With `price=None`: ts, the twelve bid/ask/mid OHLC columns and v."""
        if price is not None and price not in PRICES:
            raise ValueError(f"price must be one of {PRICES}")
        columns = COLUMNS if price else FULL_COLUMNS
        series = await self.series(source, instrument)
        if series is None:
            return empty_frame(columns)
        names = [f"{price}_{x}" for x in "ohlc"] if price else _PRICE_NAMES
        sql = (
            f"SELECT ts, {_decoded(names, series.scale)}, volume FROM {TABLES[granularity]}"
            " WHERE series=? AND ts>=? AND ts<? ORDER BY ts"
        )
        return await self._frame(sql, (series.id, start, end), columns)

    async def read_m1_frame(
        self, source: str, instrument: str, start: int, end: int, price: str = "mid"
    ) -> pd.DataFrame:
        return await self.read_frame(source, instrument, start, end, price, "M1")

    async def h1_boundary(self, source: str, instrument: str) -> int | None:
        """Where M1 takes over from H1: the first whole hour covered by M1.

        None means no M1 at all (H1 is used everywhere).
        """
        first_m1 = await self.first_ts(source, instrument, "M1")
        if first_m1 is None:
            return None
        return -(-first_m1 // 3600) * 3600  # round up to the hour

    async def first_base_ts(self, source: str, instrument: str, use_h1: bool) -> int | None:
        firsts = [await self.first_ts(source, instrument, "M1")]
        if use_h1:
            firsts.append(await self.first_ts(source, instrument, "H1"))
        known = [f for f in firsts if f is not None]
        return min(known) if known else None

    async def read_base_frame(
        self,
        source: str,
        instrument: str,
        start: int,
        end: int,
        price: str | None = "mid",
        use_h1: bool = True,
    ) -> pd.DataFrame:
        """Base rows for building 1h and higher bars: H1 before M1 coverage, M1 from there.

        The switch happens at a whole hour, so H1 rows and M1 rows never overlap and
        together cover the range once. Without `use_h1`, M1 only.
        """
        if not use_h1:
            return await self.read_frame(source, instrument, start, end, price, "M1")
        boundary = await self.h1_boundary(source, instrument)
        if boundary is None:
            return await self.read_frame(source, instrument, start, end, price, "H1")
        parts = []
        if start < boundary:
            parts.append(
                await self.read_frame(source, instrument, start, min(end, boundary), price, "H1")
            )
        if end > boundary:
            parts.append(
                await self.read_frame(source, instrument, max(start, boundary), end, price, "M1")
            )
        parts = [p for p in parts if not p.empty]
        if not parts:
            return empty_frame(COLUMNS if price else FULL_COLUMNS)
        return pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    # Cache of derived bars

    async def cache_last_ts(self, source: str, instrument: str, timeframe: str) -> int | None:
        series = await self.series(source, instrument)
        if series is None:
            return None
        return await self._scalar(
            "SELECT MAX(ts) FROM candles_cache WHERE series=? AND timeframe=?",
            series.id,
            timeframe,
        )

    async def cache_count(self, source: str, instrument: str, timeframe: str) -> int:
        series = await self.series(source, instrument)
        if series is None:
            return 0
        n = await self._scalar(
            "SELECT COUNT(*) FROM candles_cache WHERE series=? AND timeframe=?",
            series.id,
            timeframe,
        )
        return n or 0

    async def write_cache(
        self,
        source: str,
        instrument: str,
        timeframe: str,
        bars: pd.DataFrame,
        refilled: tuple[int, int] | None = None,
    ) -> int:
        """Upsert complete derived bars (a frame with FULL_COLUMNS). `refilled`: the
        `cache_dirty` range these bars recompute, removed in the same transaction."""
        if refilled is not None:
            await self.conn.execute(
                "DELETE FROM cache_dirty WHERE source=? AND instrument=? AND timeframe=?"
                " AND start_ts=? AND end_ts=?",
                (source, instrument, timeframe, *refilled),
            )
        if bars.empty:
            await self.conn.commit()
            return 0
        series = await self.series(source, instrument, create=True)
        assert series is not None
        prices = bars[FULL_COLUMNS[1:13]].to_numpy(dtype=np.float64)
        ints = np.rint(prices * series.scale).astype(np.int64)
        inexact = int((ints / series.scale != prices).any(axis=1).sum())
        self._check_exact(source, instrument, series.scale, inexact)
        stamps = bars["ts"].to_numpy(dtype=np.int64)
        volumes = bars["v"].to_numpy(dtype=np.int64)
        rows = [
            (series.id, timeframe, int(ts), *map(int, row), int(v))
            for ts, row, v in zip(stamps, ints, volumes, strict=True)
        ]
        await self.conn.executemany(_upsert_sql("candles_cache", "series, timeframe, ts"), rows)
        await self.conn.commit()
        return len(rows)

    async def read_cache(
        self,
        source: str,
        instrument: str,
        timeframe: str,
        start: int,
        end: int,
        price: str = "mid",
        limit: int | None = None,
        latest: bool = False,
    ) -> pd.DataFrame:
        """Cached bars in `[start, end)` for one price; the first or (`latest`) last `limit`."""
        if price not in PRICES:
            raise ValueError(f"price must be one of {PRICES}")
        series = await self.series(source, instrument)
        if series is None:
            return empty_frame(COLUMNS)
        names = [f"{price}_{x}" for x in "ohlc"]
        sql = (
            f"SELECT ts, {_decoded(names, series.scale)}, volume FROM candles_cache"
            " WHERE series=? AND timeframe=? AND ts>=? AND ts<?"
            f" ORDER BY ts {'DESC' if latest else 'ASC'}"
        )
        params: tuple = (series.id, timeframe, start, end)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        frame = await self._frame(sql, params, COLUMNS)
        if latest and not frame.empty:
            frame = frame.iloc[::-1].reset_index(drop=True)
        return frame

    async def clear_cache(self, source: str | None = None, instrument: str | None = None) -> None:
        which = "(? IS NULL OR source=?) AND (? IS NULL OR instrument=?)"
        params = (source, source, instrument, instrument)
        await self.conn.execute(
            f"DELETE FROM candles_cache WHERE series IN (SELECT id FROM series WHERE {which})",
            params,
        )
        await self.conn.execute(f"DELETE FROM cache_dirty WHERE {which}", params)
        await self.conn.commit()

    # Instruments

    async def upsert_instrument(
        self, source: str, info: InstrumentInfo, hours: TradingHours | None, enabled: bool = True
    ) -> None:
        await self.conn.execute(
            "INSERT INTO instruments (instrument, source, provider_symbol, display_name,"
            " decimals, pip_size, enabled, hours_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source, instrument) DO UPDATE SET"
            " provider_symbol=excluded.provider_symbol, display_name=excluded.display_name,"
            " decimals=excluded.decimals, pip_size=excluded.pip_size,"
            " enabled=excluded.enabled, hours_json=excluded.hours_json",
            (
                info.instrument,
                source,
                info.provider_symbol,
                info.display_name,
                info.decimals,
                info.pip_size,
                int(enabled),
                hours.to_json() if hours else None,
            ),
        )
        await self.conn.commit()

    async def instruments(
        self, source: str | None = None, enabled_only: bool = False
    ) -> list[InstrumentRow]:
        sql = (
            "SELECT source, instrument, provider_symbol, display_name, decimals, pip_size,"
            " enabled, hours_json, m1_depth FROM instruments WHERE (? IS NULL OR source=?)"
        )
        if enabled_only:
            sql += " AND enabled=1"
        async with self.conn.execute(sql + " ORDER BY instrument", (source, source)) as cur:
            rows = await cur.fetchall()
        return [
            InstrumentRow(
                r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), TradingHours.from_json(r[7]), r[8]
            )
            for r in rows
        ]

    async def set_m1_depth(self, source: str, instrument: str, depth: str | None) -> bool:
        """An instrument's minute history ('3m', '1y', '2y', None for the default). False if
        the instrument is unknown."""
        from openchart.retention import M1_DEPTHS

        if depth is not None and depth not in M1_DEPTHS:
            raise ValueError(f"minute history must be one of {list(M1_DEPTHS)}, got {depth!r}")
        cur = await self.conn.execute(
            "UPDATE instruments SET m1_depth=? WHERE source=? AND instrument=?",
            (depth, source, instrument),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_instrument_enabled(self, source: str, instrument: str, enabled: bool) -> bool:
        """Show or hide an instrument. Its stored history is kept. False if unknown."""
        cur = await self.conn.execute(
            "UPDATE instruments SET enabled=? WHERE source=? AND instrument=?",
            (int(enabled), source, instrument),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def has_data(self, source: str) -> bool:
        """Any stored candles from this source (any instrument, M1 or H1)."""
        for table in ("candles_m1", "candles_h1"):
            async with self.conn.execute(
                f"SELECT 1 FROM {table} WHERE series IN (SELECT id FROM series WHERE source=?)"
                " LIMIT 1",
                (source,),
            ) as cur:
                if await cur.fetchone():
                    return True
        return False

    # Learned closures (holidays, early closes)

    async def closures(self, source: str, instrument: str) -> list[tuple[int, int]]:
        async with self.conn.execute(
            "SELECT start_ts, end_ts FROM closures WHERE source=? AND instrument=?"
            " ORDER BY start_ts",
            (source, instrument),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def replace_closures(
        self, source: str, instrument: str, start: int, end: int, intervals: list[tuple[int, int]]
    ) -> bool:
        """Make `intervals` the closures that start in `[start, end)`. True if that changed them."""
        current = [c for c in await self.closures(source, instrument) if start <= c[0] < end]
        new = sorted(intervals)
        if current == new:
            return False
        await self.conn.execute(
            "DELETE FROM closures WHERE source=? AND instrument=? AND start_ts>=? AND start_ts<?",
            (source, instrument, start, end),
        )
        await self.conn.executemany(
            "INSERT OR REPLACE INTO closures (source, instrument, start_ts, end_ts)"
            " VALUES (?, ?, ?, ?)",
            [(source, instrument, s, e) for s, e in new],
        )
        await self.conn.commit()
        return True

    async def closures_scanned(self, source: str, instrument: str) -> bool:
        at = await self._scalar(
            "SELECT closures_scanned_at FROM instruments WHERE source=? AND instrument=?",
            source,
            instrument,
        )
        return at is not None

    async def mark_closures_scanned(self, source: str, instrument: str, at: int) -> None:
        await self.conn.execute(
            "UPDATE instruments SET closures_scanned_at=? WHERE source=? AND instrument=?",
            (at, source, instrument),
        )
        await self.conn.commit()

    # Downloads (newest to oldest, resumable)

    async def download_marks(self, source: str, instrument: str) -> tuple[int | None, int | None]:
        """(h1_complete_from, m1_complete_from): where each finished download ended, or
        None while it is not finished."""
        async with self.conn.execute(
            "SELECT h1_complete_from, m1_complete_from FROM instruments"
            " WHERE source=? AND instrument=?",
            (source, instrument),
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def mark_download_complete(
        self, source: str, instrument: str, granularity: str, start: int | None
    ) -> None:
        column = {"H1": "h1_complete_from", "M1": "m1_complete_from"}[granularity]
        await self.conn.execute(
            f"UPDATE instruments SET {column}=? WHERE source=? AND instrument=?",
            (start, source, instrument),
        )
        await self.conn.commit()

    async def purge_instrument(self, source: str, instrument: str) -> dict[str, int]:
        """Delete an instrument and everything stored for its prices (candles, cache,
        closures). Rows deleted per table."""
        counts: dict[str, int] = {}
        series = await self.series(source, instrument)
        async with self._writing(source, instrument, None):
            for table in ("candles_m1", "candles_h1", "candles_cache"):
                if series is None:
                    counts[table] = 0
                    continue
                cur = await self.conn.execute(f"DELETE FROM {table} WHERE series=?", (series.id,))
                counts[table] = cur.rowcount
            for table in ("series", "cache_dirty", "closures", "instruments"):
                cur = await self.conn.execute(
                    f"DELETE FROM {table} WHERE source=? AND instrument=?", (source, instrument)
                )
                counts[table] = cur.rowcount
            await self.conn.commit()
            self._series.pop((source, instrument), None)
        return counts

    # Minute history depth (storage steps 2): minutes that ended at or before a limit

    async def count_m1_before(
        self, source: str, instrument: str, cutoff: int
    ) -> tuple[int, int | None, int | None]:
        """(count, oldest, newest) of the minutes `purge_m1_before(cutoff)` would delete."""
        series = await self.series(source, instrument)
        if series is None:
            return 0, None, None
        async with self.conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles_m1 WHERE series=? AND ts<=?",
            (series.id, cutoff - STEP_SECONDS["M1"]),
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1], row[2]) if row and row[0] else (0, None, None)

    async def purge_m1_before(self, source: str, instrument: str, cutoff: int) -> int:
        """Delete the minutes that ended at or before `cutoff` (`ts + 60 <= cutoff`): nothing
        newer is touched. H1 is kept. Returns the minutes deleted.

        The hour where M1 takes over from H1 moves forward, so the cached 4h/D/W bars from
        the oldest deleted minute to the hour after the new first minute are dropped and
        recomputed (`_invalidate_cache`). The epoch moves before the delete is queued and
        after it is committed, so a read under way is not kept by a cache."""
        series = await self.series(source, instrument)
        if series is None:
            return 0
        first = await self.first_ts(source, instrument)
        if first is None or first + STEP_SECONDS["M1"] > cutoff:
            return 0
        key = (source, instrument)
        self._epoch[key] += 1
        try:
            cur = await self.conn.execute(
                "DELETE FROM candles_m1 WHERE series=? AND ts<=?",
                (series.id, cutoff - STEP_SECONDS["M1"]),
            )
            deleted = cur.rowcount
            new_first = await self.first_ts(source, instrument)
            boundary = cutoff if new_first is None else -(-new_first // 3600) * 3600
            await self._invalidate_cache(source, instrument, first, max(boundary, cutoff))
            await self.conn.commit()
        finally:
            self._epoch[key] += 1
        return deleted

    async def instrument(self, source: str, instrument: str) -> InstrumentRow | None:
        for row in await self.instruments(source):
            if row.instrument == instrument:
                return row
        return None
