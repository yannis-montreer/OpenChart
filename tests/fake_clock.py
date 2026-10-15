"""A pytest plugin that moves the process's real clock, for testing independence from it.

    FAKE_WALL_CLOCK=2026-10-14T20:59:30 FAKE_WALL_JUMP=60 \
        python -m pytest -p tests.fake_clock <tests>

The clock shows that time (UTC) from the moment the plugin is imported, which is before
`conftest.py`, and then runs forward at the normal rate: it is the clock a CI run would have
seen had it started then. It replaces `time.time` and, for the `openchart` modules,
`datetime.now`/`utcnow`. (Replacing `datetime.datetime` itself stops pytest from starting.)

`tests/test_clock_independence.py` runs a test under it. `tests/clock.py` is the other half:
the timeline the suite itself uses, which this must not be able to move.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

_real = time.time
_offset = (
    dt.datetime.fromisoformat(os.environ["FAKE_WALL_CLOCK"]).replace(tzinfo=dt.UTC).timestamp()
    - _real()
)


def _fake() -> float:
    return _real() + _offset


class _Datetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(_fake(), tz)

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(_fake(), dt.UTC).replace(tzinfo=None)


time.time = _fake


def _patch_modules() -> None:
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] == "openchart" and getattr(module, "datetime", None) is dt.datetime:
            module.datetime = _Datetime


def pytest_collection_modifyitems(session, config, items):
    _patch_modules()


def pytest_collection_finish(session):
    """`FAKE_WALL_JUMP` seconds pass between collecting the tests and running them, as on a
    slow runner (the failure this guards against needed the break to fall in between)."""
    global _offset
    _offset += float(os.environ.get("FAKE_WALL_JUMP", "0"))


def pytest_runtest_setup(item):
    _patch_modules()
