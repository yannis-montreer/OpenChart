from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from openchart.market.aggregate import Bar, resample, ticks_frame
from openchart.market.instruments import METAL_HOURS
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Tick
from openchart.providers.fake import synthetic_m1

NY = ZoneInfo("America/New_York")


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def frame(rows):
    return pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])


def m1_frame(candles, price="mid"):
    return frame([(c.ts, *getattr(c, price), c.volume) for c in candles])


def test_resample_rule():
    t0 = utc(2024, 7, 10, 12, 0)
    rows = [
        (t0, 10, 12, 9, 11, 3),
        (t0 + 60, 11, 15, 10, 14, 4),
        (t0 + 120, 14, 14, 8, 9, 5),
        (t0 + 300, 9, 10, 9, 10, 1),  # next 5m bar
    ]
    out = resample(frame(rows), TIMEFRAMES["5m"])
    assert out.to_dict("records") == [
        {"ts": t0, "o": 10, "h": 15, "l": 8, "c": 9, "v": 12},
        {"ts": t0 + 300, "o": 9, "h": 10, "l": 9, "c": 10, "v": 1},
    ]


def test_empty_frame_resamples_to_empty():
    out = resample(frame([]), TIMEFRAMES["1D"])
    assert out.empty and list(out.columns) == ["ts", "o", "h", "l", "c", "v"]


def test_daily_bars_over_a_month_with_dst_change():
    # March 2024 (DST change on the 10th): gold M1 for every open minute.
    candles = synthetic_m1(utc(2024, 3, 1), utc(2024, 3, 23), METAL_HOURS)
    days = resample(m1_frame(candles), TIMEFRAMES["1D"])
    for ts in days["ts"]:
        local = datetime.fromtimestamp(int(ts), NY)
        assert (local.hour, local.minute) == (17, 0)
        assert local.weekday() in (6, 0, 1, 2, 3)  # bars open Sunday to Thursday 17:00
    # Each full trading day of gold has 23 hours of minutes (one hour daily break).
    full = days[(days["ts"] > utc(2024, 3, 2)) & (days["ts"] < utc(2024, 3, 21))]
    assert set(full["v"] // 10) == {23 * 60}
    weeks = resample(m1_frame(candles), TIMEFRAMES["1W"])
    assert len(weeks) == 4  # the week of Mar 1 starts on Feb 23 17:00 NY


def test_resampling_is_consistent_across_levels():
    candles = synthetic_m1(utc(2024, 10, 28), utc(2024, 11, 9), METAL_HOURS)  # DST on Nov 3
    m1 = m1_frame(candles)
    via_h4 = resample(resample(m1, TIMEFRAMES["4h"]), TIMEFRAMES["1D"])
    direct = resample(m1, TIMEFRAMES["1D"])
    pd.testing.assert_frame_equal(via_h4, direct)


def test_ticks_to_5s_bars():
    t0 = utc(2024, 7, 10, 12, 0)
    ts = [t0 + 0.1, t0 + 1.2, t0 + 4.9, t0 + 5.0, t0 + 9.99]
    prices = [1.0, 3.0, 2.0, 5.0, 4.0]
    out = resample(ticks_frame(ts, prices), TIMEFRAMES["5s"])
    assert out.to_dict("records") == [
        {"ts": t0, "o": 1.0, "h": 3.0, "l": 1.0, "c": 2.0, "v": 3},
        {"ts": t0 + 5, "o": 5.0, "h": 5.0, "l": 4.0, "c": 4.0, "v": 2},
    ]


def test_bar_from_ticks_and_merge():
    bar = Bar.from_tick(60, Tick("X", 60.5, 1.0, 1.2))
    bar.add_tick(Tick("X", 61, 0.9, 1.1))
    bar.add_tick(Tick("X", 62, 1.5, 1.7))
    assert bar.bid == [1.0, 1.5, 0.9, 1.5] and bar.volume == 3
    assert bar.mid == [1.1, 1.6, 1.0, 1.6]
    later = Bar.from_tick(120, Tick("X", 120, 2.0, 2.2))
    merged = bar.merged(later)
    assert merged.ts == 60 and merged.bid == [1.0, 2.0, 0.9, 2.0] and merged.volume == 4
    assert bar.bid == [1.0, 1.5, 0.9, 1.5]  # merged() does not modify the original
