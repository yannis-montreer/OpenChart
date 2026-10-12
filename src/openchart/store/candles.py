"""M1 candle and instrument storage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import aiosqlite
import pandas as pd

from openchart.market.aggregate import COLUMNS, PRICES, empty_frame
from openchart.market.sessions import TradingHours
from openchart.providers.base import OHLC, Candle, InstrumentInfo

_UPSERT_M1 = """
INSERT INTO candles_m1 (source, instrument, ts,
    bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c,
    mid_o, mid_h, mid_l, mid_c, volume)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, instrument, ts) DO UPDATE SET
    bid_o=excluded.bid_o, bid_h=excluded.bid_h, bid_l=excluded.bid_l, bid_c=excluded.bid_c,
    ask_o=excluded.ask_o, ask_h=excluded.ask_h, ask_l=excluded.ask_l, ask_c=excluded.ask_c,
    mid_o=excluded.mid_o, mid_h=excluded.mid_h, mid_l=excluded.mid_l, mid_c=excluded.mid_c,
    volume=excluded.volume
"""


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

    async def upsert_m1(self, source: str, instrument: str, candles: Iterable[Candle]) -> int:
        """Insert or replace complete M1 candles in one transaction. Returns rows written."""
        rows = [
            (source, instrument, c.ts, *c.bid, *c.ask, *c.mid, c.volume)
            for c in candles
            if c.complete
        ]
        if rows:
            await self.conn.executemany(_UPSERT_M1, rows)
            await self.conn.commit()
        return len(rows)

    async def _scalar(self, sql: str, *params: object) -> int | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def count(self, source: str, instrument: str) -> int:
        n = await self._scalar(
            "SELECT COUNT(*) FROM candles_m1 WHERE source=? AND instrument=?", source, instrument
        )
        return n or 0

    async def first_ts(self, source: str, instrument: str) -> int | None:
        return await self._scalar(
            "SELECT MIN(ts) FROM candles_m1 WHERE source=? AND instrument=?", source, instrument
        )

    async def last_ts(self, source: str, instrument: str) -> int | None:
        return await self._scalar(
            "SELECT MAX(ts) FROM candles_m1 WHERE source=? AND instrument=?", source, instrument
        )

    async def timestamps(
        self, source: str, instrument: str, start: int | None = None, end: int | None = None
    ) -> list[int]:
        sql = "SELECT ts FROM candles_m1 WHERE source=? AND instrument=? AND ts>=? AND ts<?"
        params = (source, instrument, start or 0, end or 2**62)
        async with self.conn.execute(sql + " ORDER BY ts", params) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def read_m1(self, source: str, instrument: str, start: int, end: int) -> list[Candle]:
        sql = (
            "SELECT ts, bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c,"
            " mid_o, mid_h, mid_l, mid_c, volume FROM candles_m1"
            " WHERE source=? AND instrument=? AND ts>=? AND ts<? ORDER BY ts"
        )
        async with self.conn.execute(sql, (source, instrument, start, end)) as cur:
            rows = await cur.fetchall()
        return [Candle(r[0], OHLC(*r[1:5]), OHLC(*r[5:9]), OHLC(*r[9:13]), r[13]) for r in rows]

    async def read_m1_frame(
        self, source: str, instrument: str, start: int, end: int, price: str = "mid"
    ) -> pd.DataFrame:
        """M1 rows in `[start, end)` for one price series: columns ts, o, h, l, c, v."""
        if price not in PRICES:
            raise ValueError(f"price must be one of {PRICES}")
        p = price
        sql = (
            f"SELECT ts, {p}_o, {p}_h, {p}_l, {p}_c, volume FROM candles_m1"
            " WHERE source=? AND instrument=? AND ts>=? AND ts<? ORDER BY ts"
        )
        async with self.conn.execute(sql, (source, instrument, start, end)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return empty_frame()
        frame = pd.DataFrame.from_records(rows, columns=COLUMNS)
        return frame.astype({"ts": "int64", "v": "int64"})

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

    async def instrument(self, source: str, instrument: str) -> InstrumentRow | None:
        for row in await self.instruments(source):
            if row.instrument == instrument:
                return row
        return None
