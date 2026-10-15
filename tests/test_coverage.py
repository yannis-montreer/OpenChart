"""`openchart stats`: the share of expected trading minutes stored. It used to sample the
hours once an hour, so an hour that opens at 17:05 (currency pairs after the daily
rollover) counted as closed and EUR_USD showed 102.9%."""

import asyncio
from datetime import UTC, datetime

from openchart.main import main
from openchart.market.gaps import coverage
from openchart.market.instruments import FX_HOURS, METAL_HOURS
from openchart.providers.fake import synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.conftest import EURO


def utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


# Sunday 2024-03-03 to Saturday 2024-03-09: one whole FX week (opens Sun 17:00 NY).
WEEK = [c.ts for c in synthetic_m1(utc(2024, 3, 3), utc(2024, 3, 9), FX_HOURS)]


def old_ratio(ts, hours):
    """The previous estimate, kept to show the bug."""
    open_hours = sum(1 for t in range(ts[0], ts[-1], 3600) if hours.is_open(t))
    return len(ts) / (open_hours * 60)


def test_a_complete_fx_week_is_exactly_100_percent():
    cov = coverage(WEEK, FX_HOURS)
    assert (cov.stored, cov.expected, cov.outside) == (len(WEEK), len(WEEK), 0)
    assert cov.ratio == 1.0


def test_the_old_estimate_went_over_100_percent():
    """History starting 0 to 4 minutes past an hour: every hourly sample fell on 17:0x
    New York, inside the rollover break, so each day lost an hour of expected minutes
    but only five of stored ones."""
    ts = [t for t in WEEK if t >= utc(2024, 3, 4, 0, 2)]  # from Monday 00:02 UTC
    assert old_ratio(ts, FX_HOURS) > 1.0
    assert coverage(ts, FX_HOURS).ratio == 1.0


def test_metal_hours_too():
    week = [c.ts for c in synthetic_m1(utc(2024, 3, 3), utc(2024, 3, 9), METAL_HOURS)]
    assert coverage(week, METAL_HOURS).ratio == 1.0


def test_holes_lower_it_by_exactly_the_missing_minutes():
    ts = WEEK[:1000] + WEEK[1090:]  # 90 minutes missing
    cov = coverage(ts, FX_HOURS)
    assert (cov.stored, cov.expected) == (len(WEEK) - 90, len(WEEK))


def test_minutes_outside_the_hours_are_reported_not_counted():
    in_break = utc(2024, 3, 5, 22, 2)  # Tue 17:02 New York: inside the rollover break
    assert not FX_HOURS.is_open(in_break)
    cov = coverage(sorted([*WEEK, in_break]), FX_HOURS)
    assert (cov.ratio, cov.outside) == (1.0, 1)


def test_learned_closures_are_not_expected():
    holiday = (utc(2024, 3, 6, 0), utc(2024, 3, 6, 6))  # six hours the broker had no data
    ts = [t for t in WEEK if not holiday[0] <= t < holiday[1]]
    assert coverage(ts, FX_HOURS).ratio < 1.0  # a hole, without the closure
    assert coverage(ts, FX_HOURS, [holiday]).ratio == 1.0


def test_without_hours_every_minute_is_expected():
    cov = coverage([0, 60, 180], None)
    assert (cov.stored, cov.expected) == (3, 4)
    assert coverage([], FX_HOURS).expected == 0


def test_stats_prints_the_exact_share(tmp_path, capsys):
    async def fill():
        async with await Database.open(tmp_path / "openchart.db") as db:
            store = CandleStore(db.conn)
            await store.upsert_instrument("fake", EURO, FX_HOURS)
            candles = synthetic_m1(utc(2024, 3, 4, 0, 2), utc(2024, 3, 9), FX_HOURS)
            await store.upsert_m1("fake", "EUR_USD", candles)

    asyncio.run(fill())
    assert main(["stats", "--data-dir", str(tmp_path)]) == 0
    assert "100.0% of expected trading minutes" in capsys.readouterr().out
