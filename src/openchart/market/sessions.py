"""Trading sessions and expected trading hours.

Forex and metals follow the New York 17:00 trading-day boundary. Hours are defined in
New York local time and converted through the IANA timezone, so DST is handled by the
timezone database rather than fixed offsets.

This module is the single definition of bar alignment:
- Sub-minute, minute and hour bars align to multiples of their length since the epoch
  (New York is a whole number of hours from UTC, so this matches local alignment).
- 4h, daily and weekly bars are anchored to the trading-day close, 17:00 New York.
  4h bars open at 17, 21, 01, 05, 09 and 13 New York time; a daily bar runs from
  17:00 to 17:00; a weekly bar starts at Friday 17:00 (so it holds Sunday's open to
  Friday's close). A bar's timestamp is its start, the same convention OANDA uses.
Bounds are computed on New York wall-clock time, so at a DST change a session bar is
an hour longer or shorter, exactly like the broker's own candles.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from openchart.market.timeframes import Kind, Timeframe

NEW_YORK = "America/New_York"

# Trading-day boundary for bar alignment.
DAY_ANCHOR_TZ = NEW_YORK
DAY_ANCHOR_HOUR = 17
WEEK_ANCHOR_WEEKDAY = 4  # Friday (Monday = 0): weeks start at Friday 17:00 New York
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEK_MINUTES = 7 * 1440


def _hhmm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _week_minute(day: str, hhmm: str) -> int:
    """Minutes since Sunday 00:00 local time."""
    return ((_DAYS.index(day.lower()) + 1) % 7) * 1440 + _hhmm(hhmm)


@dataclass(frozen=True)
class TradingHours:
    """Weekly session (open day/time to close day/time) with optional daily breaks.

    JSON form (stored in `instruments.hours_json`):
        {"tz": "America/New_York",
         "open": {"day": "sun", "time": "18:00"},
         "close": {"day": "fri", "time": "17:00"},
         "daily_breaks": [{"start": "17:00", "end": "18:00"}]}
    """

    tz: str
    open_day: str
    open_time: str
    close_day: str
    close_time: str
    daily_breaks: tuple[tuple[str, str], ...] = field(default=())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradingHours:
        return cls(
            tz=data.get("tz", NEW_YORK),
            open_day=data["open"]["day"],
            open_time=data["open"]["time"],
            close_day=data["close"]["day"],
            close_time=data["close"]["time"],
            daily_breaks=tuple((b["start"], b["end"]) for b in data.get("daily_breaks", [])),
        )

    @classmethod
    def from_json(cls, text: str | None) -> TradingHours | None:
        return cls.from_dict(json.loads(text)) if text else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tz": self.tz,
            "open": {"day": self.open_day, "time": self.open_time},
            "close": {"day": self.close_day, "time": self.close_time},
            "daily_breaks": [{"start": s, "end": e} for s, e in self.daily_breaks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def is_open(self, ts: float) -> bool:
        local = datetime.fromtimestamp(ts, ZoneInfo(self.tz))
        minute_of_day = local.hour * 60 + local.minute
        week_minute = ((local.weekday() + 1) % 7) * 1440 + minute_of_day
        start = _week_minute(self.open_day, self.open_time)
        end = _week_minute(self.close_day, self.close_time)
        in_week = (start <= week_minute < end) if start < end else not (end <= week_minute < start)
        if not in_week:
            return False
        for b_start, b_end in self.daily_breaks:
            s, e = _hhmm(b_start), _hhmm(b_end)
            if (s <= minute_of_day < e) if s < e else not (e <= minute_of_day < s):
                return False
        return True


# Bar alignment

_ANCHOR_ZONE = ZoneInfo(DAY_ANCHOR_TZ)
# Shifting New York wall time by this much puts the trading-day boundary at midnight.
_SHIFT = timedelta(hours=24 - DAY_ANCHOR_HOUR)
_SHIFTED_WEEK_START = (WEEK_ANCHOR_WEEKDAY + 1) % 7  # Friday 17:00 -> Saturday 00:00
_SESSION_STEP = {"4h": timedelta(hours=4), "1D": timedelta(days=1), "1W": timedelta(days=7)}


def _session_floor(shifted: datetime, tf: Timeframe) -> datetime:
    """Start of the bar containing `shifted` (naive, shifted New York wall time)."""
    if tf.name == "4h":
        return shifted.replace(hour=shifted.hour // 4 * 4, minute=0, second=0, microsecond=0)
    day = shifted.replace(hour=0, minute=0, second=0, microsecond=0)
    if tf.name == "1D":
        return day
    if tf.name == "1W":
        return day - timedelta(days=(day.weekday() - _SHIFTED_WEEK_START) % 7)
    raise ValueError(f"{tf.name} is not a session timeframe")


def _to_epoch(shifted: datetime) -> int:
    # fold=0: an ambiguous wall time (DST fall-back) resolves to its first, DST occurrence.
    return int((shifted - _SHIFT).replace(tzinfo=_ANCHOR_ZONE).timestamp())


def bar_bounds(ts: float, tf: Timeframe) -> tuple[int, int]:
    """`(start, end)` in epoch seconds of the `tf` bar containing `ts`. End is exclusive."""
    if tf.kind is not Kind.SESSION:
        start = int(ts) - int(ts) % tf.seconds
        return start, start + tf.seconds
    local = datetime.fromtimestamp(ts, _ANCHOR_ZONE).replace(tzinfo=None)
    floor = _session_floor(local + _SHIFT, tf)
    return _to_epoch(floor), _to_epoch(floor + _SESSION_STEP[tf.name])


def bar_start(ts: float, tf: Timeframe) -> int:
    return bar_bounds(ts, tf)[0]


def bar_starts(ts: np.ndarray, tf: Timeframe) -> np.ndarray:
    """Vectorised `bar_start` for an array of epoch seconds (int64)."""
    ts = np.asarray(ts, dtype=np.int64)
    if tf.kind is not Kind.SESSION:
        return ts - ts % tf.seconds
    if ts.size == 0:
        return ts
    utc = pd.to_datetime(ts, unit="s", utc=True)
    shifted = utc.tz_convert(DAY_ANCHOR_TZ).tz_localize(None) + _SHIFT
    if tf.name == "4h":
        floor = shifted.floor("4h")
    elif tf.name == "1D":
        floor = shifted.normalize()
    elif tf.name == "1W":
        day = shifted.normalize()
        floor = day - pd.to_timedelta((day.weekday - _SHIFTED_WEEK_START) % 7, unit="D")
    else:
        raise ValueError(f"{tf.name} is not a session timeframe")
    local = (floor - _SHIFT).tz_localize(
        DAY_ANCHOR_TZ,
        ambiguous=np.ones(len(floor), dtype=bool),  # same rule as fold=0 above
        nonexistent="shift_forward",
    )
    epoch = pd.Timestamp(0, tz="UTC")
    return np.asarray((local - epoch) // pd.Timedelta(seconds=1), dtype=np.int64)
