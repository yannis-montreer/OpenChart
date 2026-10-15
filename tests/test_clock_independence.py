"""The suite must not depend on when it runs.

Two kinds of test failed depending on the time of day (see tests/clock.py):

- `test_websocket_higher_timeframe_starts_from_history` failed on every Windows job of two pull
  requests that started a minute before 17:00 New York: its history was built from the clock
  when the tests were collected, its tick from the clock when it ran, and the daily break fell
  in between;
- the drawings, alert-freeze and EUR/USD 4h tests failed on a Saturday or on a Sunday before
  the market opens: their points and bars were placed "a few hours ago", which was inside the
  weekly closure.

Here those tests run in a child pytest whose real clock is moved (tests/fake_clock.py) to
moments around the breaks and the weekly close and open, with time passing between collection
and the tests as on a slow runner. They have to pass at all of them. With the real clock left
in place (`OPENCHART_TEST_REAL_CLOCK=1`, the old behaviour) the drawings test has to fail on a
Saturday, which proves this test can still see the problem.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRAWINGS = "tests/test_drawings.py::test_create_list_update_delete"
TARGETS = [
    "tests/test_api.py::test_websocket_higher_timeframe_starts_from_history",
    DRAWINGS,
    "tests/test_alerts_api.py::test_a_cross_during_a_freeze_fires_late_with_a_freeze_message",
    "tests/test_instruments.py::test_eurusd_4h_bars_follow_its_own_hours",
]

# The daily break is 17:00 New York: 21:00 UTC in summer time, 22:00 UTC in winter time (US
# clocks go back on Sunday 2026-11-01 at 06:00 UTC); the week closes on Friday at 17:00 and
# opens on Sunday at 18:00. The clock shows `collected` while the tests are collected and
# `jump` seconds more by the time they run.
MOMENTS = {
    "summer break": ("2026-10-14T20:59:30", 60),  # collected before 21:00, runs after
    "winter break": ("2026-12-02T21:59:30", 60),  # collected before 22:00, runs after
    "summer break, collected just after it": ("2026-10-14T21:00:20", 30),
    "friday, the weekly close": ("2026-10-16T20:59:30", 60),
    "saturday": ("2026-10-17T12:00:00", 60),
    "sunday, before the weekly open": ("2026-10-18T21:58:30", 60),
    "the night US clocks go back": ("2026-11-01T05:59:30", 60),
}


def run(targets: list[str], collected: str, jump: int, *, real_clock: bool = False):
    env = {**os.environ, "FAKE_WALL_CLOCK": collected, "FAKE_WALL_JUMP": str(jump)}
    env.pop("OPENCHART_TEST_REAL_CLOCK", None)
    if real_clock:
        env["OPENCHART_TEST_REAL_CLOCK"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    command += ["-p", "tests.fake_clock", *targets]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("moment", list(MOMENTS))
def test_the_time_sensitive_tests_pass_whenever_the_run_starts(moment):
    collected, jump = MOMENTS[moment]
    done = run(TARGETS, collected, jump)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-500:]


def test_with_the_real_clock_the_drawings_test_fails_on_a_saturday_so_this_test_can_see_it():
    collected, jump = MOMENTS["saturday"]
    done = run([DRAWINGS], collected, jump, real_clock=True)
    assert done.returncode != 0, "the old behaviour did not fail: this test no longer sees it"
    assert "KeyError: 'type'" in done.stdout  # the API refused the points: a closed market


def test_with_the_real_clock_the_drawings_test_passes_in_mid_session():
    done = run([DRAWINGS], "2026-10-14T15:00:00", 60, real_clock=True)
    assert done.returncode == 0, done.stdout[-1500:]
