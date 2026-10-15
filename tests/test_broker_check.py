"""Stored M1 checked against the broker's candles: the hour before a stop, a dropped
stream or a sleep (option A), and the previous UTC day once a day (option B).

The owner's gold chart kept a tick-built minute (2026-10-13 18:00 UTC, a different high
and volume) because the server stopped before the once-a-minute refresh replaced it:
gap fill only adds newer candles and repair only fills holes.
"""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from openchart.market import feed as feed_module
from openchart.market.backfill import check_against_broker, check_day, check_tail
from openchart.market.feed import DAY_CHECK_KEY, Feed
from openchart.providers.base import OHLC
from openchart.providers.fake import FakeProvider, synthetic_m1
from openchart.settings import SettingsStore
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

GOLD = "XAU_USD"


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield CandleStore(db.conn)


def tick_built(candle):
    """What a minute built from streamed ticks looks like: the stream is sampled, so the
    high and the volume differ a little from the broker's own candle."""
    m = candle.mid
    return replace(candle, mid=OHLC(m.o, m.h - 0.05, m.l, m.c), volume=candle.volume - 3)


async def stored(store, ts):
    [candle] = await store.read_m1("fake", GOLD, ts, ts + 60)
    return candle


# Option A: the hour before the newest stored minute, on start and reconnect


async def test_start_replaces_the_tick_built_minutes_left_by_a_stop(store):
    now = int(time.time()) // 60 * 60
    broker = synthetic_m1(now - 6 * 3600, now)
    stop = now - 3 * 3600  # the server stopped three hours ago
    before_stop = [c for c in broker if c.ts < stop]
    # The last two minutes were flushed from ticks at the stop, before any refresh.
    local = before_stop[:-2] + [tick_built(c) for c in before_stop[-2:]]
    await store.upsert_m1("fake", GOLD, local)
    provider = FakeProvider({GOLD: broker})

    await Feed(provider, store, [GOLD], hours={GOLD: None}).fill_history()

    for c in before_stop[-2:]:
        assert await stored(store, c.ts) == c  # the broker's version now
    assert await store.count("fake", GOLD) == len(broker)  # and gap fill added the rest


async def test_the_tail_check_writes_only_what_differs(store):
    now = int(time.time()) // 60 * 60
    broker = synthetic_m1(now - 2 * 3600, now)
    local = [*broker[:-3], tick_built(broker[-3]), broker[-1]]  # one wrong, one missing
    extra = replace(broker[0], ts=broker[0].ts - 30 * 60)  # a minute the broker lacks
    await store.upsert_m1("fake", GOLD, [*local, extra])
    provider = FakeProvider({GOLD: broker})

    result = await check_tail(provider, store, GOLD)

    assert (result.fetched, result.replaced, result.added) == (60, 1, 1)
    assert provider.fetch_calls == 1
    assert await stored(store, broker[-3].ts) == broker[-3]
    assert await stored(store, broker[-2].ts) == broker[-2]
    assert await stored(store, extra.ts) == extra  # kept: the broker has none for it


async def test_the_tail_check_needs_history(store):
    assert await check_tail(FakeProvider({GOLD: []}), store, GOLD) is None


async def test_a_reconnect_checks_the_tail_too(store):
    now = int(time.time()) // 60 * 60
    broker = synthetic_m1(now - 3600, now)
    await store.upsert_m1("fake", GOLD, [*broker[:-1], tick_built(broker[-1])])
    feed = Feed(FakeProvider({GOLD: broker}), store, [GOLD], hours={GOLD: None})
    await feed._on_connected(reconnect=True)
    assert await stored(store, broker[-1].ts) == broker[-1]


async def test_a_failed_tail_check_does_not_stop_the_start(store, caplog):
    now = int(time.time()) // 60 * 60
    broker = synthetic_m1(now - 3600, now)
    await store.upsert_m1("fake", GOLD, broker[:-10])

    class Flaky(FakeProvider):
        calls = 0

        async def fetch_candles(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("network cut")
            async for c in super().fetch_candles(*args, **kwargs):
                yield c

    await Feed(Flaky({GOLD: broker}), store, [GOLD], hours={GOLD: None}).fill_history()
    assert "checking recent M1 of XAU_USD against the broker failed" in caplog.text
    assert await store.last_ts("fake", GOLD) == broker[-1].ts  # gap fill still ran


# Option A: on waking, the hour before the pause


async def test_waking_replaces_the_minutes_closed_before_the_pause(store, monkeypatch):
    now = int(time.time()) // 60 * 60
    broker = synthetic_m1(now - 3 * 3600, now)
    stale = broker[-5]  # closed a few minutes before the computer went to sleep
    await store.upsert_m1("fake", GOLD, [*broker[:-5], tick_built(stale), *broker[-4:]])
    feed = Feed(
        FakeProvider({GOLD: broker}), store, [GOLD], hours={GOLD: None}, refresh_interval=1e9
    )
    real = time.time
    jumps = iter([0.0, 7200.0])  # the loop's first second lasts two hours
    monkeypatch.setattr(feed_module.time, "time", lambda: real() + next(jumps, 7200.0))
    task = asyncio.create_task(feed._periodic())
    try:
        async with asyncio.timeout(5):
            while await stored(store, stale.ts) != stale:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# Option B: the previous UTC day, once a day


def utc(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


async def two_days(store):
    """Broker candles for Tue 2024-03-05 and Wed 2024-03-06; the store's Tuesday has three
    tick-built minutes and one missing."""
    broker = synthetic_m1(int(utc(2024, 3, 5)), int(utc(2024, 3, 7)))
    tuesday = [c for c in broker if c.ts < utc(2024, 3, 6)]
    local = list(tuesday)
    for i in (10, 500, 1439):
        local[i] = tick_built(local[i])
    del local[700]
    await store.upsert_m1("fake", GOLD, local)
    return broker, tuesday


async def test_the_previous_day_is_checked_and_fixed(store):
    broker, tuesday = await two_days(store)
    result = await check_day(FakeProvider({GOLD: broker}), store, GOLD, date(2024, 3, 5))
    assert (result.fetched, result.replaced, result.added) == (1440, 3, 1)
    assert await store.read_m1("fake", GOLD, int(utc(2024, 3, 5)), int(utc(2024, 3, 6))) == tuesday


async def test_the_feed_checks_each_day_once(store):
    broker, _ = await two_days(store)
    provider = FakeProvider({GOLD: broker})
    feed = Feed(provider, store, [GOLD], hours={GOLD: None})

    await feed.check_previous_day(now=utc(2024, 3, 6, 9))  # Wednesday morning
    assert provider.fetch_calls == 1
    marks = await SettingsStore(store.conn).get(DAY_CHECK_KEY)
    assert marks == {"fake:XAU_USD": "2024-03-05"}

    await feed.check_previous_day(now=utc(2024, 3, 6, 17))  # later that day, or a restart
    assert provider.fetch_calls == 1

    await feed.check_previous_day(now=utc(2024, 3, 7, 0, 20))  # Thursday: Wednesday's turn
    assert provider.fetch_calls == 2
    assert (await SettingsStore(store.conn).get(DAY_CHECK_KEY))["fake:XAU_USD"] == "2024-03-06"


async def test_the_day_is_checked_once_the_broker_has_closed_it(store):
    broker, _ = await two_days(store)
    provider = FakeProvider({GOLD: broker})
    feed = Feed(provider, store, [GOLD], hours={GOLD: None})
    await feed.check_previous_day(now=utc(2024, 3, 6, 0, 10))  # 00:10: still Monday's turn
    assert (await SettingsStore(store.conn).get(DAY_CHECK_KEY))["fake:XAU_USD"] == "2024-03-04"
    await feed.check_previous_day(now=utc(2024, 3, 6, 0, 16))  # 00:16: Tuesday
    assert (await SettingsStore(store.conn).get(DAY_CHECK_KEY))["fake:XAU_USD"] == "2024-03-05"


async def test_a_failed_day_check_is_tried_again(store, caplog):
    broker, tuesday = await two_days(store)

    class Down(FakeProvider):
        down = True

        async def fetch_candles(self, *args, **kwargs):
            if self.down:
                raise OSError("network cut")
            async for c in super().fetch_candles(*args, **kwargs):
                yield c

    provider = Down({GOLD: broker})
    feed = Feed(provider, store, [GOLD], hours={GOLD: None})
    await feed.check_previous_day(now=utc(2024, 3, 6, 9))
    assert "checking 2024-03-05 of XAU_USD against the broker failed" in caplog.text
    assert await SettingsStore(store.conn).get(DAY_CHECK_KEY) is None
    provider.down = False
    await feed.check_previous_day(now=utc(2024, 3, 6, 9, 1))
    assert await store.read_m1("fake", GOLD, int(utc(2024, 3, 5)), int(utc(2024, 3, 6))) == tuesday


async def test_instruments_still_downloading_or_empty_are_not_checked(store):
    broker, _ = await two_days(store)
    provider = FakeProvider({GOLD: broker, "EUR_USD": broker})
    feed = Feed(provider, store, [GOLD, "EUR_USD"], hours={GOLD: None, "EUR_USD": None})
    feed.downloading.add(GOLD)
    await feed.check_previous_day(now=utc(2024, 3, 6, 9))
    assert provider.fetch_calls == 0  # gold is downloading, EUR_USD has no history


async def test_check_against_broker_on_an_empty_answer_changes_nothing(store):
    broker = synthetic_m1(int(utc(2024, 3, 5)), int(utc(2024, 3, 5, 1)))
    await store.upsert_m1("fake", GOLD, broker)
    result = await check_against_broker(
        FakeProvider({GOLD: []}),
        store,
        GOLD,
        datetime(2024, 3, 5, tzinfo=UTC),
        datetime(2024, 3, 5, 1, tzinfo=UTC),
    )
    assert (result.fetched, result.replaced, result.added) == (0, 0, 0)
    assert await store.count("fake", GOLD) == 60
