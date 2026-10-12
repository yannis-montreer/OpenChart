from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from openchart.market.sessions import bar_bounds, bar_start, bar_starts
from openchart.market.timeframes import TIMEFRAMES

NY = ZoneInfo("America/New_York")
T = TIMEFRAMES


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def ny(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, NY)


@pytest.mark.parametrize("name", ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h"])
def test_fixed_timeframes_align_to_their_length(name):
    ts = utc(2024, 7, 10, 13, 47, 29)
    start, end = bar_bounds(ts, T[name])
    assert start % T[name].seconds == 0
    assert start <= ts < end
    assert end - start == T[name].seconds


@pytest.mark.parametrize("month", [1, 7])  # winter (EST) and summer (EDT)
def test_4h_bars_open_at_17_21_01_05_09_13_new_york(month):
    hours = set()
    for hour in range(24):
        start, end = bar_bounds(utc(2024, month, 10, hour, 30), T["4h"])
        hours.add(ny(start).hour)
        assert end - start == 4 * 3600
        assert ny(start).minute == 0
    assert hours == {17, 21, 1, 5, 9, 13}


def test_daily_bar_runs_17_to_17_new_york():
    # Wednesday 2024-01-10 23:30 UTC = 18:30 EST: belongs to Thursday's trading day.
    start, end = bar_bounds(utc(2024, 1, 10, 23, 30), T["1D"])
    assert ny(start) == datetime(2024, 1, 10, 17, tzinfo=NY)
    assert ny(end) == datetime(2024, 1, 11, 17, tzinfo=NY)
    # 16:59 NY is still the previous day.
    assert bar_start(utc(2024, 1, 10, 21, 59), T["1D"]) == utc(2024, 1, 9, 22)


def test_sunday_open_belongs_to_mondays_bar():
    sunday_evening = utc(2024, 7, 14, 22, 5)  # 18:05 EDT, gold reopens
    monday = utc(2024, 7, 15, 12, 0)
    assert bar_start(sunday_evening, T["1D"]) == bar_start(monday, T["1D"])
    assert ny(bar_start(monday, T["1D"])) == datetime(2024, 7, 14, 17, tzinfo=NY)


def test_daily_bars_across_spring_dst_change():
    # US DST began Sunday 2024-03-10 02:00. Before: 17:00 NY = 22:00 UTC; after: 21:00 UTC.
    friday = bar_bounds(utc(2024, 3, 8, 12), T["1D"])
    assert friday == (utc(2024, 3, 7, 22), utc(2024, 3, 8, 22))
    monday = bar_bounds(utc(2024, 3, 11, 12), T["1D"])
    assert monday == (utc(2024, 3, 10, 21), utc(2024, 3, 11, 21))
    # The weekend bar that contains the switch is 23 hours long.
    sunday = bar_bounds(utc(2024, 3, 10, 12), T["1D"])
    assert sunday[1] - sunday[0] == 23 * 3600


def test_daily_bars_across_autumn_dst_change():
    # US DST ended Sunday 2024-11-03 02:00: 17:00 NY moves from 21:00 to 22:00 UTC.
    friday = bar_bounds(utc(2024, 11, 1, 12), T["1D"])
    assert friday == (utc(2024, 10, 31, 21), utc(2024, 11, 1, 21))
    monday = bar_bounds(utc(2024, 11, 4, 12), T["1D"])
    assert monday == (utc(2024, 11, 3, 22), utc(2024, 11, 4, 22))
    sunday = bar_bounds(utc(2024, 11, 3, 12), T["1D"])
    assert sunday[1] - sunday[0] == 25 * 3600


def test_4h_bars_after_dst_change_keep_new_york_hours():
    for ts in (utc(2024, 3, 11, 14), utc(2024, 11, 4, 14)):  # Monday after each change
        assert ny(bar_start(ts, T["4h"])).hour == 9


def test_weekly_bar_starts_friday_17_new_york():
    start, end = bar_bounds(utc(2024, 7, 10, 12), T["1W"])
    assert ny(start) == datetime(2024, 7, 5, 17, tzinfo=NY)
    assert ny(end) == datetime(2024, 7, 12, 17, tzinfo=NY)
    # Sunday's open and Friday's last minute are in the same weekly bar.
    assert bar_start(utc(2024, 7, 7, 22, 0), T["1W"]) == start
    assert bar_start(utc(2024, 7, 12, 20, 59), T["1W"]) == start


def test_weekly_bars_across_dst_are_167_and_169_hours():
    spring = bar_bounds(utc(2024, 3, 11, 12), T["1W"])
    autumn = bar_bounds(utc(2024, 11, 4, 12), T["1W"])
    assert spring[1] - spring[0] == 167 * 3600
    assert autumn[1] - autumn[0] == 169 * 3600


@pytest.mark.parametrize("name", list(TIMEFRAMES))
def test_vectorised_matches_scalar_including_dst_weekends(name):
    rng = np.random.default_rng(42)
    lo, hi = utc(2023, 1, 1), utc(2027, 1, 1)
    stamps = rng.integers(lo, hi, 20_000)
    # Dense samples around both 2024 DST changes, including the repeated 01:00 hour.
    for anchor in (utc(2024, 3, 10, 0), utc(2024, 11, 3, 0)):
        stamps = np.concatenate([stamps, np.arange(anchor, anchor + 2 * 86400, 600)])
    tf = TIMEFRAMES[name]
    expected = np.array([bar_start(int(t), tf) for t in stamps])
    np.testing.assert_array_equal(bar_starts(stamps, tf), expected)


@pytest.mark.parametrize("name", ["4h", "1D", "1W"])
def test_bars_tile_time_without_gaps_or_overlaps(name):
    tf = TIMEFRAMES[name]
    ts = utc(2024, 2, 20)
    while ts < utc(2024, 12, 1):
        start, end = bar_bounds(ts, tf)
        assert start <= ts < end
        assert bar_bounds(end, tf)[0] == end  # next bar starts where this one ends
        ts = end
