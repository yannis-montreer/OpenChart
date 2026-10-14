import asyncio
import time
from datetime import UTC, datetime

import pytest

from openchart.market.cache import CandleCache
from openchart.market.feed import Feed
from openchart.providers.base import Tick
from openchart.providers.fake import STALL, FakeProvider, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from openchart.store.ticks import TickWriter, read_ticks


async def test_feed_backfills_streams_and_gap_fills_after_reconnect(tmp_path):
    now = int(time.time()) // 60 * 60
    candles = synthetic_m1(now - 3 * 86400, now)  # no hours: every minute exists
    early = candles[:-120]  # the last 2h "arrive" during the outage

    t1, t2 = Tick("XAU_USD", now + 0.25, 1.0, 1.1), Tick("XAU_USD", now + 1.5, 1.0, 1.2)
    provider = FakeProvider({"XAU_USD": early}, sessions=[[t1, OSError("network cut")], [t2]])

    async def deliver_late_candles(_):
        provider.candles["XAU_USD"] = candles

    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        await store.upsert_m1("fake", "XAU_USD", early[:100])  # history from an earlier run
        writer = TickWriter(tmp_path / "ticks", provider.name)
        feed = Feed(provider, store, ["XAU_USD"], tick_writer=writer, backfill_years=0.01)
        feed.stream._sleep = deliver_late_candles  # the "outage" is the backoff sleep

        task = asyncio.create_task(feed.run())
        async with asyncio.timeout(5):
            while feed.tick_count < 2:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # The start gap-filled the early candles, the reconnect the rest.
        assert await store.last_ts("fake", "XAU_USD") == candles[-1].ts
        assert await store.first_ts("fake", "XAU_USD") >= now - 86400 * 4
        assert feed.stream.reconnects == 1

    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    stored = read_ticks(tmp_path / "ticks", "fake", "XAU_USD", day)
    assert stored.num_rows == 2  # flushed on shutdown


class SlowEuro(FakeProvider):
    """EUR_USD history only arrives when `release` is set; records stream requests."""

    def __init__(self, *args, fail_first: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = asyncio.Event()
        self.fail_first = fail_first
        self.streamed: list[list[str]] = []

    async def fetch_candles(self, instrument, start, end, granularity="M1"):
        if instrument == "EUR_USD" and granularity == "M1":
            await self.release.wait()
            if self.fail_first:
                self.fail_first -= 1
                raise OSError("network cut")
        async for c in super().fetch_candles(instrument, start, end, granularity):
            yield c

    async def stream_ticks(self, instruments):
        self.streamed.append(list(instruments))
        async for event in super().stream_ticks(instruments):
            yield event


async def wait_for(condition, seconds=5.0):
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("fail_first", [0, 1])
async def test_a_new_instrument_downloads_in_the_background_on_the_one_stream(tmp_path, fail_first):
    """Adding EUR_USD next to gold: gold keeps streaming while EUR_USD's history downloads,
    both come through one price stream, and a failed download is retried, not fatal."""
    now = int(time.time()) // 60 * 60
    gold = synthetic_m1(now - 2 * 86400, now)
    euro = synthetic_m1(now - 2 * 86400, now, base=1.08)
    ticks = [Tick("XAU_USD", now + 1, 2650.1, 2650.4), Tick("EUR_USD", now + 2, 1.08001, 1.08013)]
    provider = SlowEuro(
        {"XAU_USD": gold, "EUR_USD": euro}, sessions=[[*ticks, STALL]], fail_first=fail_first
    )
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        await store.upsert_m1("fake", "XAU_USD", gold)
        before = await store.count("fake", "XAU_USD")
        cache = CandleCache(store)
        feed = Feed(provider, store, ["XAU_USD", "EUR_USD"], cache=cache, backfill_years=0.01)
        feed.initial_retry = 0.01
        task = asyncio.create_task(feed.run())
        try:
            # Live ticks of both flow while EUR_USD's history is still on its way.
            await wait_for(lambda: feed.tick_count == 2)
            assert provider.streamed == [["XAU_USD", "EUR_USD"]]  # one stream, both names
            assert "EUR_USD" in feed.initial
            assert feed.history["XAU_USD"]["state"] == "done"
            assert await store.count("fake", "EUR_USD") == 0
            assert await store.cache_last_ts("fake", "EUR_USD", "4h") is None  # not yet
            provider.release.set()
            await wait_for(lambda: feed.history["EUR_USD"]["state"] == "done")
            assert await store.count("fake", "EUR_USD") == len(euro)
            assert await store.count("fake", "XAU_USD") == before  # gold untouched
            assert feed.initial == set()
            assert provider.stream_calls == 1  # never restarted for the download
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
