"""Data provider interface.

Every data source (OANDA, later MT5, cTrader, Dukascopy) implements `Provider`. The rest
of the system only sees this module's types. Providers speak in canonical instrument
names (for example `XAU_USD`) and map to their own symbols internally; the mapping is
exposed through `InstrumentInfo.provider_symbol`.

All timestamps are UTC epoch seconds. Candles use integer seconds (bar open time);
ticks keep sub-second precision as a float.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Protocol, runtime_checkable

MID_DECIMALS = 9


class OHLC(NamedTuple):
    o: float
    h: float
    l: float  # noqa: E741
    c: float


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int  # bar open time
    bid: OHLC
    ask: OHLC
    mid: OHLC
    volume: int  # tick volume
    complete: bool = True


@dataclass(frozen=True, slots=True)
class Tick:
    instrument: str
    ts: float
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        # Rounded only to drop float noise (2050.1 + 2050.4) / 2 = 2050.2499999999995.
        # 9 places is finer than any instrument's precision.
        return round((self.bid + self.ask) / 2, MID_DECIMALS)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Keep-alive from the stream. Lets callers tell a quiet market from a dead link."""

    ts: float


StreamEvent = Tick | Heartbeat


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    instrument: str  # canonical name, e.g. XAU_USD
    provider_symbol: str  # the provider's own name, e.g. XAU_USD, XAUUSD, GOLD
    display_name: str
    decimals: int
    pip_size: float
    kind: str = ""  # provider category, e.g. CURRENCY, METAL, CFD


@dataclass(frozen=True, slots=True)
class TestResult:
    __test__ = False  # not a pytest test class

    ok: bool
    message: str


class ProviderError(Exception):
    """Base class for provider failures."""


class AuthError(ProviderError):
    """Credentials rejected. Retrying will not help."""


class RequestError(ProviderError):
    """A request was rejected (bad instrument, bad parameters). Not retried."""


@runtime_checkable
class Provider(Protocol):
    name: str

    async def list_instruments(self) -> list[InstrumentInfo]:
        """Instruments this account can access, with metadata."""
        ...

    def fetch_candles(
        self, instrument: str, start: datetime, end: datetime, granularity: str = "M1"
    ) -> AsyncIterator[Candle]:
        """Complete candles with `start <= ts < end`, in ascending order.

        Implementations paginate and retry transient errors internally.
        """
        ...

    def stream_ticks(self, instruments: list[str]) -> AsyncIterator[StreamEvent]:
        """Live ticks plus heartbeats from one connection.

        The iterator ends or raises when the connection drops. Reconnecting is the
        caller's job (see `openchart.providers.stream`).
        """
        ...

    async def test_connection(self) -> TestResult:
        """One live call that proves the credentials work."""
        ...

    async def aclose(self) -> None: ...
