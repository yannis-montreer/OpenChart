import asyncio
import time
from datetime import UTC, datetime

import pytest

from openchart.market.cache import CandleCache
from openchart.market.feed import Feed
from openchart.providers.base import InstrumentInfo, Tick
from openchart.providers.fake import STALL, FakeProvider, aggregate_candles, synthetic_m1
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
        self.pages: list[tuple[str, str]] = []

    async def fetch_page_before(self, instrument, end, granularity="M1"):
        if instrument == "EUR_USD":
            await self.release.wait()
            if self.fail_first:
                self.fail_first -= 1
                raise OSError("network cut")
            self.pages.append((granularity, "latest" if end is None else "older"))
        return await super().fetch_page_before(instrument, end, granularity)

    async def stream_ticks(self, instruments):
        self.streamed.append(list(instruments))
        async for event in super().stream_ticks(instruments):
            yield event


async def wait_for(condition, seconds=5.0):
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def instruments(store, complete: dict[str, bool]):
    for name, done in complete.items():
        info = InstrumentInfo(name, name, name, 3, 0.01, "METAL")
        await store.upsert_instrument("fake", info, None)
        if done:
            await store.mark_download_complete("fake", name, "H1", 0)
            await store.mark_download_complete("fake", name, "M1", 0)


@pytest.mark.parametrize("fail_first", [0, 1])
async def test_a_new_instrument_downloads_in_the_background_on_the_one_stream(tmp_path, fail_first):
    """Adding EUR_USD next to gold: gold keeps streaming while EUR_USD's history downloads,
    newest first (H1, then M1), both come through one price stream, and a failed
    download is retried, not fatal."""
    now = int(time.time()) // 60 * 60
    gold = synthetic_m1(now - 2 * 86400, now)
    euro = synthetic_m1(now - 2 * 86400, now, base=1.08)
    ticks = [Tick("XAU_USD", now + 1, 2650.1, 2650.4), Tick("EUR_USD", now + 2, 1.08001, 1.08013)]
    provider = SlowEuro(
        {"XAU_USD": gold, "EUR_USD": euro},
        h1={"EUR_USD": aggregate_candles(euro, 3600)},
        sessions=[[*ticks, STALL]],
        fail_first=fail_first,
    )
    provider.page_size = 1000
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        await instruments(store, {"XAU_USD": True, "EUR_USD": False})
        await store.upsert_m1("fake", "XAU_USD", gold)
        before = await store.read_m1("fake", "XAU_USD", 0, 2**40)
        cache = CandleCache(store)
        feed = Feed(provider, store, ["XAU_USD", "EUR_USD"], cache=cache, backfill_years=0.01)
        feed.download_retry = 0.01
        task = asyncio.create_task(feed.run())
        try:
            # Live ticks of both flow while EUR_USD's history is still on its way.
            await wait_for(lambda: feed.tick_count == 2)
            assert provider.streamed == [["XAU_USD", "EUR_USD"]]  # one stream, both names
            assert feed.downloading == {"EUR_USD"}
            assert feed.history["XAU_USD"]["state"] == "done"
            assert await store.count("fake", "EUR_USD") == 0
            provider.release.set()
            await wait_for(lambda: feed.history["EUR_USD"]["state"] == "done")
            await wait_for(lambda: feed._background.done())  # then closures and cache
            assert await store.count("fake", "EUR_USD") == len(euro)
            assert await store.count("fake", "EUR_USD", "H1") == len(provider.h1["EUR_USD"])
            assert await store.read_m1("fake", "XAU_USD", 0, 2**40) == before  # untouched
            assert feed.downloading == set()
            assert provider.stream_calls == 1  # never restarted for the download
            # H1 first, then M1, each from the present backward.
            phases = [g for g, _ in provider.pages]
            assert phases == sorted(phases, key=lambda g: g != "H1")
            assert next(w for g, w in provider.pages if g == "M1") == "latest"
            marks = await store.download_marks("fake", "EUR_USD")
            assert marks[0] == provider.h1["EUR_USD"][0].ts and marks[1] is not None
            assert await store.cache_last_ts("fake", "EUR_USD", "4h") is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
