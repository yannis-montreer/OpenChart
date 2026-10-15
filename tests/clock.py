"""One clock for the whole test run.

The tests used to take "now" from the real clock: `conftest.py` built three days of stored
history up to the minute it was imported, and tests then pushed ticks stamped with the clock
of the moment they ran. A run that crossed the daily break (17:00 New York) between the two,
such as CI starting a minute before it, put the tick in a new trading day with none of the
stored history in it, and `test_websocket_higher_timeframe_starts_from_history` failed
(and with it every Windows job of two pull requests). The same clock decides what is "today"
for tick files, which bars are complete, and which trading day a live candle belongs to, so
the failure was about when the suite ran, not about anything a change had done.

So the process under test gets a clock of its own: it starts at `START`, the middle of a
trading day, and runs forward at the normal rate. It replaces `time.time` and, for the
`openchart` modules, `datetime.now`/`utcnow`; the tests and the server see the same time.
Waits (`time.monotonic`, `asyncio.sleep`, `anyio.fail_after`) are not touched.

`OPENCHART_TEST_REAL_CLOCK=1` leaves the real clock in place. Only
`tests/test_clock_independence.py` uses it, to prove it can still see the old failure.
`tests/fake_clock.py` moves the real clock, to test that none of this depends on it.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

# Wednesday 2026-10-14, 15:00 UTC = 11:00 in New York (summer time): six hours before the daily
# break at 17:00 and a day and a half away from the weekend. A run of a few minutes, and the
# three days of history before it, stay inside ordinary trading days.
START = dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC).timestamp()

_wall = time.time  # the process's own clock; under tests/fake_clock.py a moved one
_offset: float | None = None


def now() -> float:
    """The test timeline's current time (seconds since the epoch)."""
    return _wall() + (_offset or 0.0)


class _Datetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(now(), tz)

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(now(), dt.UTC).replace(tzinfo=None)


def install() -> None:
    """Start the test timeline at `START`. Call once, before anything reads the time."""
    global _offset
    if os.environ.get("OPENCHART_TEST_REAL_CLOCK") or _offset is not None:
        return
    _offset = START - _wall()
    time.time = now
    patch_modules()


def patch_modules() -> None:
    """Point `datetime` in the openchart modules at the timeline's class. Cheap; the autouse
    fixture calls it before each test, so a module imported late is covered too."""
    if _offset is None:
        return
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] == "openchart" and getattr(module, "datetime", None) is dt.datetime:
            module.datetime = _Datetime
