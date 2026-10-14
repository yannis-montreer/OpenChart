from datetime import UTC, datetime

from openchart.market.gaps import find_gaps
from openchart.market.instruments import FX_HOURS, METAL_HOURS
from openchart.market.sessions import TradingHours


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def test_gold_weekly_open_follows_new_york_dst():
    # Winter (EST, UTC-5): Sunday 18:00 NY = 23:00 UTC.
    assert not METAL_HOURS.is_open(ts(2024, 1, 7, 22, 59))
    assert METAL_HOURS.is_open(ts(2024, 1, 7, 23, 0))
    # Summer (EDT, UTC-4): Sunday 18:00 NY = 22:00 UTC.
    assert not METAL_HOURS.is_open(ts(2024, 7, 7, 21, 59))
    assert METAL_HOURS.is_open(ts(2024, 7, 7, 22, 0))


def test_gold_weekly_close_and_daily_break():
    # Friday 17:00 NY in summer = 21:00 UTC.
    assert METAL_HOURS.is_open(ts(2024, 7, 12, 20, 59))
    assert not METAL_HOURS.is_open(ts(2024, 7, 12, 21, 0))
    assert not METAL_HOURS.is_open(ts(2024, 7, 13, 12, 0))  # Saturday
    # Daily break 17:00-18:00 NY on a Wednesday in winter = 22:00-23:00 UTC.
    assert METAL_HOURS.is_open(ts(2024, 1, 10, 21, 59))
    assert not METAL_HOURS.is_open(ts(2024, 1, 10, 22, 30))
    assert METAL_HOURS.is_open(ts(2024, 1, 10, 23, 0))


def test_fx_pauses_five_minutes_for_the_daily_rollover():
    # Wednesday in winter: 17:00-17:05 New York = 22:00-22:05 UTC; gold's hour is open.
    assert FX_HOURS.is_open(ts(2024, 1, 10, 21, 59))
    assert not FX_HOURS.is_open(ts(2024, 1, 10, 22, 0))
    assert not FX_HOURS.is_open(ts(2024, 1, 10, 22, 4))
    assert FX_HOURS.is_open(ts(2024, 1, 10, 22, 5))
    assert FX_HOURS.is_open(ts(2024, 1, 10, 22, 30))
    # Summer (EDT): 20:59 UTC open, 21:00-21:04 closed, 21:05 open.
    assert FX_HOURS.is_open(ts(2024, 7, 10, 20, 59))
    assert not FX_HOURS.is_open(ts(2024, 7, 10, 21, 4))
    assert FX_HOURS.is_open(ts(2024, 7, 10, 21, 5))
    # The week opens at 17:05 New York on Sunday.
    assert not FX_HOURS.is_open(ts(2024, 1, 7, 22, 0))
    assert FX_HOURS.is_open(ts(2024, 1, 7, 22, 5))


def test_dst_change_weekend():
    # US DST started Sunday 2024-03-10: open is 18:00 EDT = 22:00 UTC that day.
    assert not METAL_HOURS.is_open(ts(2024, 3, 10, 21, 59))
    assert METAL_HOURS.is_open(ts(2024, 3, 10, 22, 0))
    # Friday before (EST): close 17:00 = 22:00 UTC.
    assert METAL_HOURS.is_open(ts(2024, 3, 8, 21, 59))
    assert not METAL_HOURS.is_open(ts(2024, 3, 8, 22, 0))


def test_hours_json_round_trip():
    assert TradingHours.from_json(METAL_HOURS.to_json()) == METAL_HOURS
    assert TradingHours.from_json(None) is None


def test_find_gaps_ignores_weekend_but_reports_outage():
    friday_close = ts(2024, 7, 12, 20, 59)
    sunday_open = ts(2024, 7, 14, 22, 0)
    stamps = [friday_close - 60, friday_close, sunday_open, sunday_open + 60]
    assert find_gaps(stamps, METAL_HOURS) == []

    outage = [ts(2024, 7, 10, 12, 0), ts(2024, 7, 10, 12, 30)]
    [gap] = find_gaps(outage, METAL_HOURS)
    assert gap.open_minutes == 29


def test_find_gaps_tolerates_quiet_minutes():
    stamps = [ts(2024, 7, 10, 3, 0), ts(2024, 7, 10, 3, 3)]  # two empty minutes
    assert find_gaps(stamps, METAL_HOURS, min_open_minutes=10) == []
