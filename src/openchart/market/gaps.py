"""Detect holes in stored M1 history that market closures do not explain."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from openchart.market.sessions import TradingHours

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
