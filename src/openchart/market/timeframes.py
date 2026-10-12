"""Supported chart timeframes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    TICK = "tick"  # sub-minute, built from raw ticks only
    FIXED = "fixed"  # fixed length, aligned to multiples of its length since the epoch
    SESSION = "session"  # anchored to the trading-day boundary (see market.sessions)


@dataclass(frozen=True)
class Timeframe:
    name: str
    seconds: int  # nominal length; session bars can be an hour longer or shorter at DST
    kind: Kind
    oanda: str | None = None  # OANDA granularity, for comparisons with broker candles

    def __str__(self) -> str:
        return self.name


_ALL = [
    Timeframe("1s", 1, Kind.TICK),
    Timeframe("5s", 5, Kind.TICK, "S5"),
    Timeframe("15s", 15, Kind.TICK, "S15"),
    Timeframe("30s", 30, Kind.TICK, "S30"),
    Timeframe("1m", 60, Kind.FIXED, "M1"),
    Timeframe("5m", 300, Kind.FIXED, "M5"),
    Timeframe("15m", 900, Kind.FIXED, "M15"),
    Timeframe("30m", 1800, Kind.FIXED, "M30"),
    Timeframe("1h", 3600, Kind.FIXED, "H1"),
    Timeframe("4h", 4 * 3600, Kind.SESSION, "H4"),
    Timeframe("1D", 86400, Kind.SESSION, "D"),
    Timeframe("1W", 7 * 86400, Kind.SESSION, "W"),
]

TIMEFRAMES: dict[str, Timeframe] = {tf.name: tf for tf in _ALL}
M1 = TIMEFRAMES["1m"]


def parse_timeframe(name: str) -> Timeframe:
    try:
        return TIMEFRAMES[name]
    except KeyError:
        raise ValueError(
            f"unknown timeframe {name!r}, expected one of {list(TIMEFRAMES)}"
        ) from None
