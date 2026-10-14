"""Deterministic in-memory provider for tests and offline development.

Candles are served from a fixed list. The stream replays scripted sessions: each call
to `stream_ticks` consumes the next session, a list of events, exceptions to raise
(simulated network errors) and `STALL` markers (the link goes silent).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Iterable
from datetime import datetime

from openchart.market.sessions import TradingHours
from openchart.providers.base import (
    OHLC,
    Candle,
    InstrumentInfo,
    StreamEvent,
    TestResult,
)

STALL = object()

Script = list[StreamEvent | BaseException | object]


def _merge(a: OHLC, b: OHLC) -> OHLC:
    return OHLC(a.o, max(a.h, b.h), min(a.l, b.l), b.c)


def aggregate_candles(candles: list[Candle], seconds: int) -> list[Candle]:
    """Combine candles into `seconds`-long epoch-aligned candles (e.g. M1 to H1)."""
    out: list[Candle] = []
    for c in candles:
        ts = c.ts - c.ts % seconds
        if out and out[-1].ts == ts:
            p = out[-1]
            out[-1] = Candle(
                ts,
                _merge(p.bid, c.bid),
                _merge(p.ask, c.ask),
                _merge(p.mid, c.mid),
                p.volume + c.volume,
            )
        else:
            out.append(Candle(ts, c.bid, c.ask, c.mid, c.volume))
    return out


def synthetic_m1(
    start_ts: int, end_ts: int, hours: TradingHours | None = None, base: float = 2000.0
) -> list[Candle]:
    """Plausible M1 candles for every open minute in `[start_ts, end_ts)`.

    Moves scale with `base` (at 2000: a swing of 10, bars 1 high, a spread of 0.2), so
    any instrument's prices look like its own.
    """
    out = []
    swing, bar, spread = base / 200, base / 4000, base / 10000
    for ts in range((start_ts // 60) * 60, end_ts, 60):
        if hours is not None and not hours.is_open(ts):
            continue
        mid = base + swing * math.sin(ts / 3600)
        m = OHLC(mid, mid + bar, mid - bar, mid + bar / 5)
        b = OHLC(*(p - spread / 2 for p in m))
        a = OHLC(*(p + spread / 2 for p in m))
        out.append(Candle(ts, b, a, m, volume=10))
    return out


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        candles: dict[str, Iterable[Candle]] | None = None,
        sessions: Iterable[Script] | None = None,
        instruments: list[InstrumentInfo] | None = None,
        h1: dict[str, Iterable[Candle]] | None = None,
    ):
        self.candles = {k: sorted(v, key=lambda c: c.ts) for k, v in (candles or {}).items()}
        self.h1 = {k: sorted(v, key=lambda c: c.ts) for k, v in (h1 or {}).items()}
        self.sessions = list(sessions or [])
        self._instruments = instruments or []
        self.fetch_calls = 0
        self.stream_calls = 0
        self.page_size = 5000  # candles per `fetch_page_before`, like OANDA's maximum

    async def list_instruments(self) -> list[InstrumentInfo]:
        return list(self._instruments)

    async def fetch_candles(
        self, instrument: str, start: datetime, end: datetime, granularity: str = "M1"
    ) -> AsyncIterator[Candle]:
        self.fetch_calls += 1
        start_ts, end_ts = start.timestamp(), end.timestamp()
        series = self.h1 if granularity == "H1" else self.candles
        for candle in series.get(instrument, []):
            if start_ts <= candle.ts < end_ts and candle.complete:
                yield candle

    async def fetch_page_before(
        self, instrument: str, end: datetime | None, granularity: str = "M1"
    ) -> list[Candle]:
        self.fetch_calls += 1
        end_ts = float("inf") if end is None else end.timestamp()
        series = self.h1 if granularity == "H1" else self.candles
        older = [c for c in series.get(instrument, []) if c.ts < end_ts and c.complete]
        return older[-self.page_size :]

    async def stream_ticks(self, instruments: list[str]) -> AsyncIterator[StreamEvent]:
        self.stream_calls += 1
        if not self.sessions:
            await asyncio.Event().wait()  # nothing scripted: stay silent
        for item in self.sessions.pop(0):
            if item is STALL:
                await asyncio.Event().wait()
            elif isinstance(item, BaseException):
                raise item
            else:
                await asyncio.sleep(0)
                yield item  # type: ignore[misc]

    async def test_connection(self) -> TestResult:
        return TestResult(True, "fake provider")

    async def aclose(self) -> None:
        pass
