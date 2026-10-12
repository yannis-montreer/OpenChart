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


def synthetic_m1(
    start_ts: int, end_ts: int, hours: TradingHours | None = None, base: float = 2000.0
) -> list[Candle]:
    """Plausible M1 candles for every open minute in `[start_ts, end_ts)`."""
    out = []
    for ts in range((start_ts // 60) * 60, end_ts, 60):
        if hours is not None and not hours.is_open(ts):
            continue
        mid = base + 10 * math.sin(ts / 3600)
        spread = 0.2
        m = OHLC(mid, mid + 0.5, mid - 0.5, mid + 0.1)
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
    ):
        self.candles = {k: sorted(v, key=lambda c: c.ts) for k, v in (candles or {}).items()}
        self.sessions = list(sessions or [])
        self._instruments = instruments or []
        self.fetch_calls = 0
        self.stream_calls = 0

    async def list_instruments(self) -> list[InstrumentInfo]:
        return list(self._instruments)

    async def fetch_candles(
        self, instrument: str, start: datetime, end: datetime, granularity: str = "M1"
    ) -> AsyncIterator[Candle]:
        self.fetch_calls += 1
        start_ts, end_ts = start.timestamp(), end.timestamp()
        for candle in self.candles.get(instrument, []):
            if start_ts <= candle.ts < end_ts and candle.complete:
                yield candle

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
