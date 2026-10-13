"""Watchdog (SPEC section 9): the alert engine must never fail silently.

- "Engine started" on every server start.
- "Feed stalled" when no tick arrived for `stall_seconds` (default 5 minutes) while the
  instrument's market should be open, and "feed recovered" when ticks are back.
- After a freeze (the computer slept) the server sends its own message; the watchdog
  only restarts its timers, so the freeze is not also reported as a stall.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

STALL_SECONDS = 5 * 60


@dataclass
class FeedView:
    last_tick: float | None  # time of the instrument's last tick
    market_open: bool  # should it be trading now (hours and learned closures)


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class Watchdog:
    def __init__(
        self, *, stall_seconds: float = STALL_SECONDS, now: Callable[[], float] = time.time
    ):
        self.stall_seconds = stall_seconds
        self.now = now
        self.since = now()  # ticks are expected from here on (start, wake-up)
        self.stalled: dict[str, float] = {}  # instrument -> since when silent

    def reset(self) -> None:
        """After a start or wake-up: give the feed time to come back."""
        self.since = self.now()
        self.stalled.clear()

    def check(self, feeds: dict[str, FeedView], state: str = "") -> list[str]:
        """Messages to send now."""
        now = self.now()
        out: list[str] = []
        for instrument, view in feeds.items():
            silent_since = max(view.last_tick or 0.0, self.since)
            silent = now - silent_since
            if instrument in self.stalled:
                if view.last_tick is not None and view.last_tick > self.stalled[instrument]:
                    began = self.stalled.pop(instrument)
                    out.append(
                        f"✅ Feed recovered: {instrument} prices are back after "
                        f"{fmt_duration(view.last_tick - began)}. Alerts are live again."
                    )
            elif view.market_open and silent >= self.stall_seconds:
                # The outage began with the last tick (if it came after the start).
                self.stalled[instrument] = view.last_tick or silent_since
                detail = f" Feed state: {state}." if state else ""
                out.append(
                    f"⚠️ Feed stalled: no {instrument} price for {fmt_duration(silent)} "
                    "while the market should be open, so alerts cannot fire."
                    f"{detail} (A market holiday also shows up like this.)"
                )
        return out
