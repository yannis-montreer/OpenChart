import asyncio
import time
from datetime import UTC, datetime, timedelta

from openchart.market.feed import Feed
from openchart.providers.base import Tick
from openchart.providers.fake import FakeProvider, synthetic_m1
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
        writer = TickWriter(tmp_path / "ticks", provider.name)
        feed = Feed(provider, store, ["XAU_USD"], tick_writer=writer, backfill_years=0.01)
        feed.stream._sleep = deliver_late_candles  # the "outage" is the backoff sleep

        task = asyncio.create_task(feed.run())
        async with asyncio.timeout(5):
            while feed.tick_count < 2:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # Initial backfill covered the early candles, the reconnect gap-filled the rest.
        assert await store.last_ts("fake", "XAU_USD") == candles[-1].ts
        assert await store.first_ts("fake", "XAU_USD") >= now - 86400 * 4
        assert feed.stream.reconnects == 1

    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    stored = read_ticks(tmp_path / "ticks", "fake", "XAU_USD", day)
    assert stored.num_rows == 2  # flushed on shutdown


async def test_feed_starts_full_backfill_when_empty(tmp_path):
    now = datetime.now(UTC)
    candles = synthetic_m1(int((now - timedelta(days=2)).timestamp()), int(now.timestamp()))
    provider = FakeProvider({"XAU_USD": candles})
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        feed = Feed(provider, store, ["XAU_USD"], backfill_years=1)
        await feed.fill_history()
        assert await store.count("fake", "XAU_USD") == len(candles)
