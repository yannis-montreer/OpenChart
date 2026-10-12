"""Trading sessions and expected trading hours.

Forex and metals follow the New York 17:00 trading-day boundary. Hours are defined in
New York local time and converted through the IANA timezone, so DST is handled by the
timezone database rather than fixed offsets.

M2 adds bar alignment (H4/D/W anchoring) to this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = "America/New_York"
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
