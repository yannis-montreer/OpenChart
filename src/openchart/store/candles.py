"""Candle and instrument storage.

Two provider granularities are stored: M1 (the source of truth for recent history) and
H1 (deep history, where M1 does not reach). Complete 4h, D and W bars derived from
them are cached in `candles_cache`; every M1 or H1 write deletes the cached bars it
could affect, so the cache never serves stale data and can always be rebuilt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import aiosqlite
import pandas as pd

from openchart.market.aggregate import COLUMNS, FULL_COLUMNS, PRICES, empty_frame
from openchart.market.sessions import TradingHours, bar_start
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import OHLC, Candle, InstrumentInfo

TABLES = {"M1": "candles_m1", "H1": "candles_h1"}
STEP_SECONDS = {"M1": 60, "H1": 3600}
CACHED_TIMEFRAMES = ("4h", "1D", "1W")

_PRICE_COLUMNS = (
    "bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, mid_o, mid_h, mid_l, mid_c"
)


def _upsert_sql(table: str, key: str = "source, instrument, ts") -> str:
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


class CandleStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        # Bumped on every M1/H1 write, before it is queued. The cache compares it to
        # detect that the data it was computing from changed underneath it.
        self._epoch: dict[tuple[str, str], int] = defaultdict(int)

    def epoch(self, source: str, instrument: str) -> int:
        return self._epoch[(source, instrument)]

    # Writes

    async def upsert(
        self, source: str, instrument: str, candles: Iterable[Candle], granularity: str = "M1"
    ) -> int:
        """Insert or replace complete candles in one transaction. Returns rows written."""
        rows = [
            (source, instrument, c.ts, *c.bid, *c.ask, *c.mid, c.volume)
            for c in candles
            if c.complete
        ]
        if not rows:
            return 0
        self._epoch[(source, instrument)] += 1
        await self.conn.executemany(_upsert_sql(TABLES[granularity]), rows)
        await self._invalidate_cache(source, instrument, min(r[2] for r in rows))
        await self.conn.commit()
        return len(rows)

    async def upsert_m1(self, source: str, instrument: str, candles: Iterable[Candle]) -> int:
        return await self.upsert(source, instrument, candles, "M1")

    async def _invalidate_cache(self, source: str, instrument: str, since: int) -> None:
        """Drop cached bars that contain `since` or come after it."""
        for name in CACHED_TIMEFRAMES:
            await self.conn.execute(
                "DELETE FROM candles_cache WHERE source=? AND instrument=? AND timeframe=?"
                " AND ts>=?",
                (source, instrument, name, bar_start(since, TIMEFRAMES[name])),
            )

    # Reads

    async def _scalar(self, sql: str, *params: object) -> int | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def count(self, source: str, instrument: str, granularity: str = "M1") -> int:
        n = await self._scalar(
            f"SELECT COUNT(*) FROM {TABLES[granularity]} WHERE source=? AND instrument=?",
            source,
            instrument,
        )
        return n or 0

    async def first_ts(self, source: str, instrument: str, granularity: str = "M1") -> int | None:
        return await self._scalar(
            f"SELECT MIN(ts) FROM {TABLES[granularity]} WHERE source=? AND instrument=?",
            source,
            instrument,
        )

    async def last_ts(
        self, source: str, instrument: str, granularity: str = "M1", *, before: int | None = None
    ) -> int | None:
        """Newest stored candle, or the newest one strictly before `before`."""
        return await self._scalar(
            f"SELECT MAX(ts) FROM {TABLES[granularity]} WHERE source=? AND instrument=? AND ts<?",
            source,
            instrument,
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
        sql = (
            f"SELECT ts FROM {TABLES[granularity]}"
            " WHERE source=? AND instrument=? AND ts>=? AND ts<? ORDER BY ts"
        )
        params = (source, instrument, start or 0, end or 2**62)
        async with self.conn.execute(sql, params) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def read(
        self, source: str, instrument: str, start: int, end: int, granularity: str = "M1"
    ) -> list[Candle]:
        sql = (
            f"SELECT ts, {_PRICE_COLUMNS}, volume FROM {TABLES[granularity]}"
            " WHERE source=? AND instrument=? AND ts>=? AND ts<? ORDER BY ts"
        )
        async with self.conn.execute(sql, (source, instrument, start, end)) as cur:
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
        cols = f"{price}_o, {price}_h, {price}_l, {price}_c" if price else _PRICE_COLUMNS
        sql = (
            f"SELECT ts, {cols}, volume FROM {TABLES[granularity]}"
            " WHERE source=? AND instrument=? AND ts>=? AND ts<? ORDER BY ts"
        )
        return await self._frame(
            sql, (source, instrument, start, end), COLUMNS if price else FULL_COLUMNS
        )

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
        return await self._scalar(
            "SELECT MAX(ts) FROM candles_cache WHERE source=? AND instrument=? AND timeframe=?",
            source,
            instrument,
            timeframe,
        )

    async def cache_count(self, source: str, instrument: str, timeframe: str) -> int:
        n = await self._scalar(
            "SELECT COUNT(*) FROM candles_cache WHERE source=? AND instrument=? AND timeframe=?",
            source,
            instrument,
            timeframe,
        )
        return n or 0

    async def write_cache(
        self, source: str, instrument: str, timeframe: str, bars: pd.DataFrame
    ) -> int:
        """Upsert complete derived bars (a frame with FULL_COLUMNS)."""
        if bars.empty:
            return 0
        values = bars[FULL_COLUMNS].to_numpy(dtype=object)
        rows = [
            (source, instrument, timeframe, int(r[0]), *map(float, r[1:13]), int(r[13]))
            for r in values
        ]
        await self.conn.executemany(
            _upsert_sql("candles_cache", "source, instrument, timeframe, ts"), rows
        )
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
        sql = (
            f"SELECT ts, {price}_o, {price}_h, {price}_l, {price}_c, volume FROM candles_cache"
            " WHERE source=? AND instrument=? AND timeframe=? AND ts>=? AND ts<?"
            f" ORDER BY ts {'DESC' if latest else 'ASC'}"
        )
        params: tuple = (source, instrument, timeframe, start, end)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        frame = await self._frame(sql, params, COLUMNS)
        if latest and not frame.empty:
            frame = frame.iloc[::-1].reset_index(drop=True)
        return frame

    async def clear_cache(self, source: str | None = None, instrument: str | None = None) -> None:
        await self.conn.execute(
            "DELETE FROM candles_cache"
            " WHERE (? IS NULL OR source=?) AND (? IS NULL OR instrument=?)",
            (source, source, instrument, instrument),
        )
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
            " enabled, hours_json FROM instruments WHERE (? IS NULL OR source=?)"
        )
        if enabled_only:
            sql += " AND enabled=1"
        async with self.conn.execute(sql + " ORDER BY instrument", (source, source)) as cur:
            rows = await cur.fetchall()
        return [
            InstrumentRow(
                r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), TradingHours.from_json(r[7])
            )
            for r in rows
        ]

    async def set_instrument_enabled(self, source: str, instrument: str, enabled: bool) -> bool:
        """Show or hide an instrument. Its stored history is kept. False if unknown."""
        cur = await self.conn.execute(
            "UPDATE instruments SET enabled=? WHERE source=? AND instrument=?",
            (int(enabled), source, instrument),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def instrument(self, source: str, instrument: str) -> InstrumentRow | None:
        for row in await self.instruments(source):
            if row.instrument == instrument:
                return row
        return None
