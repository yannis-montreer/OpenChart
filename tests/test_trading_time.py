"""Trading clock and line geometry, and the fixture that keeps the frontend identical."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openchart.alerts.geometry import line_price, time_range
from openchart.market.instruments import FX_HOURS, METAL_HOURS
from openchart.market.sessions import TradingClock, trading_clock
from tests.conftest import ODD_HOURS

FIXTURE = Path(__file__).resolve().parent.parent / "frontend/src/drawings/trading-time.fixture.json"


def utc(*a) -> float:
    return datetime(*a, tzinfo=UTC).timestamp()


def sample_times() -> list[int]:
    out = []
    # Every 37 minutes through both 2024 US DST changes, weekends and daily breaks.
    for start in (utc(2024, 3, 6), utc(2024, 10, 30)):
        out += [int(start) + 37 * 60 * i for i in range(10 * 24 * 60 // 37)]
    out += [int(utc(2024, 7, 12, 20, 59, 30)), int(utc(2024, 7, 12, 21, 0)), 1_800_000_000]
    return out


# Learned closures for the fixture: an early close, a closure spanning a weekend, and two
# overlapping ones (merged), all inside the sampled periods.
CLOSURES = [
    (int(utc(2024, 3, 7, 18, 30)), int(utc(2024, 3, 7, 22))),
    (int(utc(2024, 3, 11, 10)), int(utc(2024, 3, 11, 11))),
    (int(utc(2024, 3, 11, 10, 30)), int(utc(2024, 3, 11, 12))),
    (int(utc(2024, 11, 1, 12)), int(utc(2024, 11, 4, 23))),
]


def fixture_data() -> dict:
    data = {}
    cases = (
        ("metal", METAL_HOURS, []),
        ("fx", FX_HOURS, []),
        ("metal_closures", METAL_HOURS, CLOSURES),
        # The made-up third instrument of the M9 tests: Berlin time, a break over midnight.
        ("odd", ODD_HOURS, []),
    )
    for name, hours, closures in cases:
        clock = TradingClock(hours, closures)
        data[name] = {
            "hours": hours.to_dict(),
            "closures": [list(c) for c in closures],
            "samples": [[t, clock(t)] for t in sample_times()],
        }
    return data


def test_frontend_fixture_is_current():
    """frontend/src/drawings/trading-time.fixture.json must match this implementation.

    Regenerate with: python -c "import tests.test_trading_time as t; t.write_fixture()"
    """
    assert json.loads(FIXTURE.read_text()) == fixture_data()


def write_fixture() -> None:
    FIXTURE.write_text(json.dumps(fixture_data(), separators=(",", ":")) + "\n")


def test_clock_runs_only_while_open():
    gold = TradingClock(METAL_HOURS)
    assert gold.week_open_seconds == 115 * 3600  # 5 days x 23 hours
    assert TradingClock(FX_HOURS).week_open_seconds == 120 * 3600
    # Friday 16:00 New York to Sunday 18:00: one trading hour (16:00-17:00).
    assert gold(utc(2024, 7, 14, 22)) - gold(utc(2024, 7, 12, 20)) == 3600
    # The daily break (17:00-18:00 New York) does not count.
    assert gold(utc(2024, 7, 15, 22)) - gold(utc(2024, 7, 15, 21)) == 0
    assert gold(utc(2024, 7, 15, 22, 30)) - gold(utc(2024, 7, 15, 22)) == 1800
    # A full trading day is 23 hours either side of a DST change.
    for day in ((2024, 3, 7), (2024, 3, 11), (2024, 10, 31), (2024, 11, 4)):
        start = utc(*day, 12)
        assert gold(start + 86400) - gold(start) == 23 * 3600


def test_clock_is_monotonic():
    gold = TradingClock(METAL_HOURS)
    values = [gold(t) for t in sample_times()[:-3]]
    assert values == sorted(values)


def test_unknown_hours_fall_back_to_clock_time():
    assert trading_clock(None)(123.5) == 123.5


# Line geometry (SPEC section 9), measured on the trading clock

A, B = {"t": 0, "p": 10.0}, {"t": 7200, "p": 20.0}


@pytest.mark.parametrize(
    ("kind", "t", "expected"),
    [
        ("extended", 3600, 15.0),
        ("extended", 14400, 30.0),
        ("trendline", 7200, 20.0),
        ("trendline", 7201, None),
        ("ray", -1, None),
        ("ray", 14400, 30.0),
        ("hline", 10**9, 10.0),
    ],
)
def test_line_price(kind, t, expected):
    pts = [A] if kind == "hline" else [A, B]
    assert line_price(kind, pts, t) == expected


def test_ray_pointing_left():
    assert time_range("ray", [B, A]) == (float("-inf"), 7200)
    assert line_price("ray", [B, A], -7200) == 0.0


def test_line_is_straight_across_the_weekend_in_trading_time():
    gold = TradingClock(METAL_HOURS)
    thu, fri = utc(2024, 7, 11, 13), utc(2024, 7, 12, 13)  # 09:00 New York
    pts = [{"t": thu, "p": 100.0}, {"t": fri, "p": 99.0}]  # -1 per trading day
    monday = utc(2024, 7, 15, 13)
    # Friday 09:00 to Monday 09:00 is one trading day (23 h), not three calendar days.
    assert line_price("extended", pts, monday, gold) == pytest.approx(98.0)
    assert line_price("extended", pts, monday) == pytest.approx(96.0)  # clock time


# Learned closures (holidays, early closes)

LABOR_DAY = (int(utc(2024, 9, 2, 17, 30)), int(utc(2024, 9, 2, 22)))  # early close


def test_the_clock_stops_during_a_closure():
    plain, clock = TradingClock(METAL_HOURS), TradingClock(METAL_HOURS, [LABOR_DAY])
    start, end = LABOR_DAY
    before, after = start - 3600, end + 3600
    assert clock(before) == plain(before)
    assert clock(start + 1800) == clock(start) == clock(end)  # flat inside
    lost = plain(end) - plain(start)
    assert lost == 3.5 * 3600  # 17:30-22:00 UTC less the daily break 21:00-22:00
    assert clock(after) - clock(before) == plain(after) - plain(before) - lost


def test_a_closure_over_a_weekend_only_removes_open_time():
    plain = TradingClock(METAL_HOURS)
    friday, monday = int(utc(2024, 3, 29, 12)), int(utc(2024, 4, 1, 23))  # Good Friday
    clock = TradingClock(METAL_HOURS, [(friday, monday)])
    lost = plain(monday) - plain(friday)
    assert clock(monday + 3600) == plain(monday + 3600) - lost
    assert clock(friday) == clock(monday)


def test_overlapping_closures_are_merged():
    a = TradingClock(METAL_HOURS, [(1000, 5000), (3000, 9000)])
    assert a.closures == [(1000, 9000)]


def test_a_line_is_straight_across_a_learned_holiday():
    start, end = LABOR_DAY
    clock = TradingClock(METAL_HOURS, [LABOR_DAY])
    a, b = start - 4 * 3600, start - 3600
    pts = [{"t": a, "p": 100.0}, {"t": b, "p": 97.0}]  # -1 per open hour
    assert line_price("extended", pts, end, clock) == pytest.approx(96.0)  # no drop inside
    assert line_price("extended", pts, end + 3600, clock) == pytest.approx(95.0)
    assert line_price("extended", pts, end, TradingClock(METAL_HOURS)) < 93.5  # without it
