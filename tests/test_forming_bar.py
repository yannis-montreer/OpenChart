"""The forming minute in indicator values.

`CandleBuilder` persists an M1 bar only when it closes, and `GET /api/candles` reads stored M1,
so the bar that is forming (and on the 1m chart every live bar) was missing from the values an
indicator was computed on: the browser showed no value for it until the minute had closed. The
indicator route now asks the reader to fold the builder's forming M1 bar into the series.
`/api/candles` itself is unchanged (the browser draws the forming bar from the websocket)."""

from datetime import UTC, datetime

import pytest

from openchart.market.aggregate import Bar
from openchart.market.instruments import METAL_HOURS
from openchart.market.series import SeriesReader
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Tick
from openchart.providers.fake import synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.conftest import NOW
from tests.test_api import push
from tests.test_indicators import candles, values

T = TIMEFRAMES


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


# Wednesday, a trading day. Stored M1 up to 10:07 (exclusive), so 10:07 is the forming minute.
DAY_START, STORED_END = utc(2024, 3, 12, 6), utc(2024, 3, 12, 10, 7)
FORMING_TS = STORED_END
STORED = synthetic_m1(DAY_START, STORED_END, METAL_HOURS)


def forming_bar(ts: int = FORMING_TS) -> Bar:
    # bid, ask and mid are each [open, high, low, close]
    return Bar(ts, [1.0, 9.0, 0.5, 2.0], [1.2, 9.2, 0.7, 2.2], [1.1, 9.1, 0.6, 2.1], 7)


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        s = CandleStore(db.conn)
        await s.upsert_m1("oanda", "XAU_USD", STORED)
        yield s


def reader(store, bar=None):
    return SeriesReader(store, live=lambda instrument: bar)


async def read(store, tf, bar, *, price="mid", **kw):
    kw.setdefault("now", FORMING_TS + 20)
    return await reader(store, bar).candles("oanda", "XAU_USD", T[tf], price=price, **kw)


async def test_the_forming_minute_is_merged_into_the_bar_of_its_period(store):
    plain = await read(store, "5m", None, forming=True)
    got = await read(store, "5m", forming_bar(), forming=True)
    assert len(got) == len(plain)
    before, after = plain.iloc[-1], got.iloc[-1]
    assert after["ts"] == before["ts"] == utc(2024, 3, 12, 10, 5)  # the 10:05 bar holds 10:07
    assert after["o"] == before["o"]
    assert after["h"] == max(before["h"], 9.1)
    assert after["l"] == min(before["l"], 0.6)
    assert after["c"] == 2.1
    assert after["v"] == before["v"] + 7
    assert not after["complete"]
    assert got.iloc[:-1].equals(plain.iloc[:-1])


async def test_a_forming_minute_that_starts_a_period_is_a_bar_of_its_own(store):
    ts = utc(2024, 3, 12, 10, 10)
    got = await read(store, "5m", forming_bar(ts), forming=True, now=ts + 20)
    plain = await read(store, "5m", None, forming=True, now=ts + 20)
    assert len(got) == len(plain) + 1
    last = got.iloc[-1]
    assert (last["ts"], last["o"], last["h"], last["l"], last["c"], last["v"]) == (
        ts,
        1.1,
        9.1,
        0.6,
        2.1,
        7,
    )
    assert not last["complete"]


async def test_on_the_one_minute_chart_the_live_bar_is_there(store):
    got = await read(store, "1m", forming_bar(), forming=True)
    assert got["ts"].iloc[-1] == FORMING_TS
    assert got["ts"].iloc[-2] == FORMING_TS - 60


@pytest.mark.parametrize("price,close", [("mid", 2.1), ("bid", 2.0), ("ask", 2.2)])
async def test_the_price_type_picks_its_own_series(store, price, close):
    got = await read(store, "1m", forming_bar(), price=price, forming=True)
    assert got["c"].iloc[-1] == close


async def test_off_by_default_so_the_candles_route_keeps_its_meaning(store):
    got = await read(store, "1m", forming_bar())
    assert got["ts"].iloc[-1] == FORMING_TS - 60


async def test_nothing_forming_changes_nothing(store):
    a = await read(store, "5m", None, forming=True)
    b = await read(store, "5m", None)
    assert a.equals(b)


async def test_a_window_that_ends_before_the_forming_minute_does_not_get_it(store):
    got = await read(store, "1m", forming_bar(), forming=True, end=FORMING_TS)
    assert got["ts"].iloc[-1] == FORMING_TS - 60


async def test_a_page_that_is_full_stops_where_it_is(store):
    start = utc(2024, 3, 12, 9)
    got = await read(store, "1m", forming_bar(), forming=True, start=start, limit=5)
    assert list(got["ts"]) == [
        start + 60 * i for i in range(5)
    ]  # full page: the live bar is beyond it


async def test_a_page_that_reaches_the_live_edge_gets_it(store):
    start = utc(2024, 3, 12, 10, 5)
    got = await read(store, "1m", forming_bar(), forming=True, start=start, limit=10)
    assert list(got["ts"]) == [start, start + 60, FORMING_TS]


async def test_the_latest_n_bars_still_number_n(store):
    got = await read(store, "1m", forming_bar(), forming=True, limit=4)
    assert len(got) == 4 and got["ts"].iloc[-1] == FORMING_TS


async def test_a_forming_bar_from_a_period_before_the_window_is_left_out(store):
    start = utc(2024, 3, 12, 10, 10)  # the window starts after the forming minute's 5m bar
    got = await read(store, "5m", forming_bar(), forming=True, start=start, limit=5, now=start + 20)
    assert (got["ts"] >= start).all()


async def test_sub_minute_bars_are_built_from_ticks_and_left_alone(store):
    # Built from the buffered ticks, which include the live ones; the live M1 is not added.
    r = SeriesReader(
        store,
        buffered_ticks=lambda i: [Tick("XAU_USD", FORMING_TS + 3, 1.0, 1.2)],
        live=lambda i: forming_bar(),
    )
    got = await r.candles(
        "oanda",
        "XAU_USD",
        T["30s"],
        forming=True,
        start=FORMING_TS,
        end=FORMING_TS + 30,
        now=FORMING_TS + 20,
    )
    assert list(got["ts"]) == [FORMING_TS]
    assert got["v"].iloc[-1] == 1


# Through the API: real ticks into the feed, a real indicator route.


def test_an_indicator_has_a_value_for_the_live_bar_on_the_one_minute_chart(client):
    q = {"timeframe": "1m", "limit": 5}
    assert candles(client, **q)[-1]["t"] == NOW - 60  # the stored bars end before the live minute
    push(client, Tick("XAU_USD", NOW + 30, 2000.0, 2000.2))
    push(client, Tick("XAU_USD", NOW + 31, 2003.0, 2003.2))
    assert candles(client, **q)[-1]["t"] == NOW - 60  # /api/candles is unchanged

    body = values(client, **q).json()
    assert body["t"][-1] == NOW
    assert body["values"]["ema"][-1] is not None
    # The forming minute's close is the last tick's mid, so the EMA moved towards it.
    assert body["values"]["ema"][-1] != body["values"]["ema"][-2]


def test_a_five_minute_bar_has_a_value_for_the_live_minute(client):
    from openchart.market.sessions import bar_bounds

    period = bar_bounds(NOW, TIMEFRAMES["5m"])[0]  # the 5m bar that holds the live minute
    push(client, Tick("XAU_USD", NOW + 30, 2000.0, 2000.2))
    body = values(client, timeframe="5m", limit=3).json()
    assert body["t"][-1] == period
    assert body["values"]["ema"][-1] is not None
    bars = candles(client, timeframe="5m", limit=3)
    assert bars[-1]["t"] in (period, period - 300)  # the stored bars: with or without that period


def test_the_values_answer_matches_the_candles_it_is_computed_on_when_nothing_is_forming(client):
    body = values(client, timeframe="1m", limit=5).json()
    assert body["t"] == [b["t"] for b in candles(client, timeframe="1m", limit=5)]


async def test_the_high_of_the_live_minute_counts_when_it_is_the_highest(store):
    high = Bar(
        FORMING_TS,
        [3000.0, 9000.0, 2999.0, 3001.0],
        [3000.0] * 4,
        [3000.0, 9000.0, 2999.0, 3001.0],
        3,
    )
    got = await read(store, "5m", high, forming=True)
    assert got["h"].iloc[-1] == 9000.0


async def test_a_live_bar_older_than_the_last_stored_one_is_ignored(store):
    # A stale builder (a missed close) must not put a bar out of order.
    old = forming_bar(FORMING_TS - 3600)
    got = await read(store, "1m", old, forming=True)
    plain = await read(store, "1m", None)
    assert got.equals(plain)
    assert got["ts"].is_monotonic_increasing
