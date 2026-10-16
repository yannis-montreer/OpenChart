"""`/api/candles` ends with the forming bar, flagged, and nothing of it is kept.

Before this, a bar was in the answer only once its first minute had closed (the builder writes an
M1 bar when it closes), so a reload drew the chart up to the last closed minute and the websocket
caught it up a moment later. Now the builder's forming minute is folded into the bar of its period
(or is a bar of its own) in memory, for the answer only:

- `forming: true` on that bar (the key is absent on every other bar) and `complete: false`;
- never written: not to the stored minutes, not to the cache of 4h, D and W bars;
- `forming=false` on the request gives the old answer.

The legend's "no value for the live bar" was the indicator route (fixed with `forming=True` there);
this is the same gap in the candles themselves.
"""

import time

import pytest

from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Tick
from tests.conftest import NOW
from tests.test_api import push
from tests.test_forming_bar import (  # noqa: F401  (fixtures and helpers)
    FORMING_TS,
    forming_bar,
    read,
    store,
)
from tests.test_indicators import candles, values


@pytest.fixture(autouse=True)
def hold_the_minute_open(request, monkeypatch):
    """These tests are about the route and the reader, not about when the feed closes a minute.

    The feed's 1 s loop closes (and stores) a forming minute once the clock is `close_grace`
    past its end, and the test timeline runs on from the start of the run, so late in a run, or
    on a slow machine, the minute of a tick pushed here can be over between the push and the
    request, or between two snapshots. Without this, a run that reached these tests a minute in
    failed one of them on CI (seen on ubuntu 3.11); under a clock moved late and a fast loop six
    failed every time."""
    if "client" in request.fixturenames:
        client = request.getfixturevalue("client")
        monkeypatch.setattr(client.server.builder, "on_clock", lambda now: None)


LIVE = [Tick("XAU_USD", NOW + 30, 2000.0, 2000.2), Tick("XAU_USD", NOW + 31, 2003.0, 2003.2)]
HUGE = [Tick("XAU_USD", NOW + 30, 9000.0, 9000.2), Tick("XAU_USD", NOW + 31, 100.0, 100.2)]


def go_live(client, ticks=LIVE):
    for tick in ticks:
        push(client, tick)


# The answer


def test_the_one_minute_chart_ends_with_the_forming_bar(client):
    before = candles(client, timeframe="1m", limit=5)
    assert before[-1]["t"] == NOW - 60 and not any("forming" in b for b in before)
    go_live(client)
    bars = candles(client, timeframe="1m", limit=5)
    assert len(bars) == 5
    last = bars[-1]
    assert last["t"] == NOW
    assert last["forming"] is True and last["complete"] is False
    assert last["c"] == pytest.approx(2003.1)  # the last tick's mid
    assert all("forming" not in b and b["complete"] is True for b in bars[:-1])


def test_a_five_minute_bar_takes_the_live_minute_into_its_own(client):
    go_live(client, HUGE)
    period = bar_bounds(NOW, TIMEFRAMES["5m"])[0]
    bars = candles(client, timeframe="5m", limit=3)
    last = bars[-1]
    assert last["t"] == period and last["forming"] is True and last["complete"] is False
    assert last["h"] == pytest.approx(9000.1) and last["l"] <= 100.1 + 1e-9
    assert last["c"] == pytest.approx(100.1)
    assert "forming" not in bars[-2]


@pytest.mark.parametrize("timeframe", ["1h", "4h", "1D", "1W"])
def test_higher_timeframes_flag_their_forming_bar_too(client, timeframe):
    go_live(client, HUGE)
    last = candles(client, timeframe=timeframe, limit=3)[-1]
    assert last["forming"] is True and last["complete"] is False
    assert last["h"] == pytest.approx(9000.1)


def test_nothing_forming_means_no_flag_anywhere(client):
    for timeframe in ("1m", "5m", "1D"):
        assert not any("forming" in b for b in candles(client, timeframe=timeframe, limit=50))


def test_forming_false_gives_the_old_answer(client):
    stored = candles(client, timeframe="1m", limit=5)
    go_live(client)
    off = client.get(
        "/api/candles",
        params={"instrument": "XAU_USD", "timeframe": "1m", "limit": 5, "forming": "false"},
    ).json()["candles"]
    assert off == stored


def test_the_indicator_route_and_the_candles_route_end_on_the_same_bar(client):
    go_live(client)
    bar = candles(client, timeframe="1m", limit=3)[-1]
    body = values(client, timeframe="1m", limit=3).json()
    assert body["t"][-1] == bar["t"]


# Windows that do not reach the live edge


def test_a_window_that_ends_before_the_live_minute_has_none(client):
    go_live(client)
    bars = candles(client, timeframe="1m", to=NOW, limit=5)
    assert bars[-1]["t"] == NOW - 60 and not any("forming" in b for b in bars)


def test_an_older_page_has_none(client):
    go_live(client)
    page = candles(client, timeframe="1m", **{"from": NOW - 3600}, limit=5)
    assert len(page) == 5 and not any("forming" in b for b in page)


def test_seconds_bars_have_no_flag(client):
    go_live(client)
    assert not any("forming" in b for b in candles(client, timeframe="30s", limit=5))


# Never stored


def settle(client) -> None:
    """Wait for the feed's startup work (the deep-history fill, which also fills the cache of
    4h, D and W bars) to finish: it writes those tables while a test runs, as fast as the machine
    lets it, and a snapshot taken meanwhile differs from the next for no reason of ours."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        task = client.server.feed._background
        if task is not None and task.done():
            return
        time.sleep(0.05)
    pytest.fail("the feed's startup work did not finish")


def snapshot(client) -> dict[str, list]:
    """Every row of every table, whole database (rows sorted: a rewrite of the same row moves
    it in the table, which is no change)."""
    settle(client)

    async def rows():
        conn = client.server.db.conn
        out = {}
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            names = sorted(r[0] for r in await cur.fetchall())
        for name in names:
            async with conn.execute(f'SELECT * FROM "{name}"') as cur:
                out[name] = sorted((tuple(r) for r in await cur.fetchall()), key=repr)
        return out

    return client.portal.call(rows)


def test_control_the_snapshot_sees_a_stored_minute(client):
    from openchart.providers.base import OHLC, Candle

    before = snapshot(client)
    bar = OHLC(1.0, 2.0, 0.5, 1.5)
    stored = [Candle(NOW, bar, bar, bar, volume=1)]
    client.portal.call(client.server.store.upsert_m1, client.server.source, "XAU_USD", stored)
    assert snapshot(client) != before


@pytest.mark.parametrize("timeframe", ["1m", "5m", "1h", "4h", "1D", "1W"])
def test_reading_the_forming_bar_writes_nothing(client, timeframe):
    go_live(client, HUGE)
    # A first read lets the cache of 4h, D and W bars catch up with the stored (complete) bars.
    plain = {"instrument": "XAU_USD", "timeframe": timeframe, "limit": 50, "forming": "false"}
    client.get("/api/candles", params=plain)
    before = snapshot(client)
    got = client.get("/api/candles", params={**plain, "forming": "true"}).json()["candles"]
    assert got[-1].get("forming") is True  # it was in the answer ...
    assert snapshot(client) == before  # ... and in no table
    again = client.get("/api/candles", params=plain).json()["candles"]
    assert not any("forming" in b for b in again)  # the next plain read does not have it
    assert all(b["h"] < 9000 for b in again)


# The reader


async def test_complete_is_false_for_a_bar_with_live_data_even_when_its_period_is_over(store):  # noqa: F811
    # The minute is over, but no tick has closed it yet (a quiet market): the bar is final in
    # content but not stored, and it must not be taken for a complete one.
    got = await read(store, "1m", forming_bar(), forming=True, now=FORMING_TS + 90)
    assert got["ts"].iloc[-1] == FORMING_TS
    assert not bool(got["complete"].iloc[-1]) and got.attrs["forming"] == FORMING_TS
    control = await read(store, "1m", None, forming=True, now=FORMING_TS + 90)
    assert bool(control["complete"].iloc[-1]) and "forming" not in control.attrs


async def test_the_flag_names_the_folded_bar_only(store):  # noqa: F811
    got = await read(store, "5m", forming_bar(), forming=True)
    assert got.attrs["forming"] == int(got["ts"].iloc[-1])
    assert got["complete"].iloc[:-1].all()


async def test_no_flag_when_the_folded_bar_is_cut_by_the_page(store):  # noqa: F811
    got = await read(store, "1m", forming_bar(), forming=True, start=FORMING_TS - 3600, limit=5)
    assert got["ts"].iloc[-1] < FORMING_TS and "forming" not in got.attrs


async def test_no_flag_for_sub_minute_bars(store):  # noqa: F811
    got = await read(
        store, "30s", forming_bar(), forming=True, start=FORMING_TS, end=FORMING_TS + 30
    )
    assert "forming" not in got.attrs


async def test_a_read_with_it_does_not_change_the_next_read_without_it(store):  # noqa: F811
    first = await read(store, "5m", None)
    await read(store, "5m", forming_bar(), forming=True)
    assert (await read(store, "5m", None)).equals(first)
