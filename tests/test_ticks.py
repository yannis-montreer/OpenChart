from datetime import UTC, datetime

from openchart.providers.base import Tick
from openchart.store.ticks import TickWriter, read_ticks

DAY1 = datetime(2024, 7, 10, 23, 59, 59, tzinfo=UTC).timestamp()


def test_flush_partitions_by_utc_day_and_compacts(tmp_path):
    writer = TickWriter(tmp_path, "oanda")
    writer.add(Tick("XAU_USD", DAY1, 2400.1, 2400.4))
    writer.add(Tick("XAU_USD", DAY1 + 1.5, 2400.2, 2400.5))  # crosses into the next day
    writer.add(Tick("EUR_USD", DAY1, 1.08, 1.0801))
    assert writer.flush() == 3
    assert writer.pending == 0

    writer.add(Tick("XAU_USD", DAY1 - 10, 2399.9, 2400.2))
    writer.flush()

    day1 = writer.day_dir("XAU_USD", "2024-07-10")
    assert len(list(day1.glob("part-*.parquet"))) == 2
    assert writer.compact_closed_days(today="2024-07-11") >= 1
    assert [p.name for p in day1.iterdir()] == ["data.parquet"]
    # The current day is left alone.
    assert list(writer.day_dir("XAU_USD", "2024-07-11").glob("part-*.parquet"))

    table = read_ticks(tmp_path, "oanda", "XAU_USD", "2024-07-10")
    assert table.column("bid").to_pylist() == [2399.9, 2400.1]  # sorted by time
    ts0 = table.column("ts")[1].as_py()
    assert abs(ts0.timestamp() - DAY1) < 1e-6  # microsecond precision kept


def test_compaction_is_repeatable(tmp_path):
    writer = TickWriter(tmp_path, "oanda")
    writer.add(Tick("XAU_USD", DAY1, 1.0, 2.0))
    writer.flush()
    writer.compact_closed_days(today="2024-07-11")
    writer.add(Tick("XAU_USD", DAY1 - 1, 0.5, 1.5))  # late tick for a closed day
    writer.flush()
    writer.compact_closed_days(today="2024-07-11")
    assert read_ticks(tmp_path, "oanda", "XAU_USD", "2024-07-10").num_rows == 2


def test_read_missing_day_is_empty(tmp_path):
    assert read_ticks(tmp_path, "oanda", "XAU_USD", "2020-01-01").num_rows == 0
