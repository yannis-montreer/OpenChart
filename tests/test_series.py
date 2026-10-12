from datetime import UTC, datetime

import pytest

from openchart.market.instruments import METAL_HOURS
from openchart.market.series import SeriesReader
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Tick
from openchart.providers.fake import synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from openchart.store.ticks import TickWriter

T = TIMEFRAMES


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


START, END = utc(2024, 3, 1), utc(2024, 3, 23)
CANDLES = synthetic_m1(START, END, METAL_HOURS)


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        s = CandleStore(db.conn)
        await s.upsert_m1("oanda", "XAU_USD", CANDLES)
        yield s


async def test_latest_bars_with_limit(store):
    reader = SeriesReader(store)
    bars = await reader.candles("oanda", "XAU_USD", T["1h"], limit=50, now=END + 3600)
    assert len(bars) == 50
    assert bars["ts"].iloc[-1] == bars["ts"].max()
    assert bars["complete"].all()


async def test_limit_reaches_back_over_weekends(store):
    reader = SeriesReader(store)
    bars = await reader.candles("oanda", "XAU_USD", T["1D"], limit=10, now=END)
    assert len(bars) == 10  # 2 weekends inside: the reader widens its window


async def test_from_returns_first_bars_and_excludes_partial_bar(store):
    reader = SeriesReader(store)
    start = utc(2024, 3, 12, 10, 7)  # mid-bar
    bars = await reader.candles("oanda", "XAU_USD", T["15m"], start=start, limit=4, now=END)
    assert list(bars["ts"]) == [utc(2024, 3, 12, 10, 15) + 900 * i for i in range(4)]


async def test_current_bar_is_flagged_incomplete(store):
    reader = SeriesReader(store)
    now = utc(2024, 3, 22, 12, 30)  # a Friday, mid trading day
    bars = await reader.candles("oanda", "XAU_USD", T["4h"], end=now, limit=3, now=now)
    assert list(bars["complete"]) == [True, True, False]


async def test_bid_ask_mid_series(store):
    reader = SeriesReader(store)
    args = dict(start=utc(2024, 3, 12), end=utc(2024, 3, 13), now=END)
    bid = await reader.candles("oanda", "XAU_USD", T["1h"], price="bid", **args)
    ask = await reader.candles("oanda", "XAU_USD", T["1h"], price="ask", **args)
    mid = await reader.candles("oanda", "XAU_USD", T["1h"], price="mid", **args)
    assert (ask["c"] > mid["c"]).all() and (mid["c"] > bid["c"]).all()


async def test_unknown_instrument_is_empty(store):
    bars = await SeriesReader(store).candles("oanda", "NOPE", T["1h"], limit=5, now=END)
    assert bars.empty


async def test_sub_minute_bars_from_tick_files_and_buffer(tmp_path, store):
    writer = TickWriter(tmp_path / "ticks", "oanda")
    t0 = utc(2024, 7, 10, 12, 0)
    writer.add(Tick("XAU_USD", t0 + 1, 10, 10.2))
    writer.add(Tick("XAU_USD", t0 + 3, 12, 12.2))
    writer.flush()
    writer.add(Tick("XAU_USD", t0 + 6, 11, 11.2))  # still in memory
    reader = SeriesReader(store, tmp_path / "ticks", writer.buffered)
    bars = await reader.candles("oanda", "XAU_USD", T["5s"], price="bid", now=t0 + 7)
    assert bars[["ts", "o", "h", "l", "c", "v"]].to_dict("records") == [
        {"ts": t0, "o": 10, "h": 12, "l": 10, "c": 12, "v": 2},
        {"ts": t0 + 5, "o": 11, "h": 11, "l": 11, "c": 11, "v": 1},
    ]
    assert list(bars["complete"]) == [True, False]
