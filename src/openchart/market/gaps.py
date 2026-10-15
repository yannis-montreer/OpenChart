"""Detect holes in stored M1 history that market closures do not explain."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from openchart.market.sessions import TradingClock, TradingHours

M1 = 60


@dataclass(frozen=True)
class Gap:
    before: int  # last candle before the hole
    after: int  # first candle after the hole
    open_minutes: int  # missing minutes during expected trading hours


def find_gaps(
    timestamps: Sequence[int], hours: TradingHours | None, min_open_minutes: int = 10
) -> list[Gap]:
    """Holes with at least `min_open_minutes` missing minutes while the market should be open.

    Single empty minutes are normal (no ticks in that minute), hence the threshold.
    Without trading hours every missing minute counts.
    """
    gaps: list[Gap] = []
    for before, after in itertools.pairwise(timestamps):
        if after - before <= M1:
            continue
        missing = range(before + M1, after, M1)
        if hours is None:
            open_minutes = len(missing)
        else:
            open_minutes = sum(1 for ts in missing if hours.is_open(ts))
        if open_minutes >= min_open_minutes:
            gaps.append(Gap(before, after, open_minutes))
    return gaps


@dataclass(frozen=True)
class Coverage:
    stored: int  # stored minutes while the market is open (per the hours and closures)
    expected: int  # open minutes from the first stored minute to the last, both included
    outside: int  # stored minutes outside the hours or inside a learned closure

    @property
    def ratio(self) -> float:
        return self.stored / self.expected if self.expected else 0.0


def coverage(
    timestamps: Sequence[int],
    hours: TradingHours | None,
    closures: Sequence[tuple[float, float]] = (),
) -> Coverage:
    """How much of the expected trading time stored M1 covers. Both counts are exact, to
    the minute: the daily breaks (17:00-17:05 for currency pairs) and learned closures
    are neither expected nor counted, so the ratio never exceeds 1."""
    ts = np.asarray(timestamps, dtype="int64")
    if not len(ts):
        return Coverage(0, 0, 0)
    if hours is None:
        expected = int((ts[-1] - ts[0]) // M1 + 1)
        return Coverage(len(ts), expected, 0)
    clock = TradingClock(hours, closures)
    expected = round((clock(int(ts[-1]) + M1) - clock(int(ts[0]))) / M1)
    local = pd.to_datetime(ts, unit="s", utc=True).tz_convert(hours.tz)
    week_minute = ((local.dayofweek + 1) % 7) * 1440 + local.hour * 60 + local.minute
    inside = np.asarray(clock.open_minutes, dtype=bool)[np.asarray(week_minute)]
    for start, end in clock.closures:
        inside &= ~((ts >= start) & (ts < end))
    stored = int(inside.sum())
    return Coverage(stored, expected, len(ts) - stored)
