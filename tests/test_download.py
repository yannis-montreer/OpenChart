"""History downloads newest to oldest (M9): OANDA's backward paging, one contiguous block
ending at the present after every page, resuming after an interruption, and the cache,
the H1/M1 boundary bar, gaps, repair and closures on a partly downloaded history."""

import time
from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest

from openchart.config import OandaConfig
from openchart.lock import DataFolderLock, in_use
from openchart.market.aggregate import resample_full
from openchart.market.backfill import deep_fill, download_backward, learn_closures, repair
from openchart.market.cache import CandleCache
from openchart.market.feed import Feed
from openchart.market.gaps import find_gaps
from openchart.market.instruments import METAL_HOURS
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import OHLC, Candle, InstrumentInfo
from openchart.providers.fake import FakeProvider, aggregate_candles, synthetic_m1
from openchart.providers.oanda import MAX_CANDLES_PER_REQUEST, OandaProvider
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.oanda_mock import TOKEN, CandleServer
from tests.test_deep_history import Clock, full_cache

GOLD = InstrumentInfo("XAU_USD", "XAU_USD", "Gold", 3, 0.01, "METAL")


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        s = CandleStore(db.conn)
        await s.upsert_instrument("oanda", GOLD, METAL_HOURS)
        await s.upsert_instrument("fake", GOLD, METAL_HOURS)
        yield s


# OANDA's backward paging


def oanda(server: CandleServer) -> OandaProvider:
    config = OandaConfig("practice", TOKEN, "101-001-1-001")
    return OandaProvider(config, transport=httpx.MockTransport(server), request_delay=0)


async def test_backward_paging_over_many_pages_skips_and_repeats_nothing(store):
    n = 3 * MAX_CANDLES_PER_REQUEST + 1234  # 4 pages and a bit
    now = int(time.time()) // 60 * 60
    stamps = [now - 60 * (n - 1 - i) for i in range(n)]  # the last one is still forming
    server = CandleServer(stamps, incomplete_last=True)
    provider = oanda(server)
    seen: list[int] = []

    result = await download_backward(
        provider, store, "XAU_USD", None, progress=lambda w, ts, d: seen.append(ts)
    )
    stored = await store.timestamps("oanda", "XAU_USD")
    assert stored == stamps[:-1]  # every complete candle once, none skipped
    assert result.complete and result.oldest == stamps[0] and result.written == n - 1
    assert len(server.requests) == 5  # 4 full pages, then an empty one: nothing older
    first, *rest = server.requests
    assert "to" not in first.url.params  # the first page is the present
    # Each next page ends at the oldest candle so far; the candle at `to` comes back
    # and counts (the quirk), so the next pages hold 4999 new ones.
    tos = [int(r.url.params["to"]) for r in rest]
    assert tos == seen[:4] and seen == sorted(seen, reverse=True)
    assert await store.download_marks("oanda", "XAU_USD") == (None, stamps[0])

    server.requests.clear()  # finished: nothing to fetch on a rerun
    assert (await download_backward(provider, store, "XAU_USD", stamps[0])).written == 0
    await provider.aclose()
    assert server.requests == []


async def test_backward_paging_stops_at_the_start_asked_for(store):
    n = 2 * MAX_CANDLES_PER_REQUEST
    now = int(time.time()) // 60 * 60
    stamps = [now - 60 * (n - i) for i in range(n)]
    server = CandleServer(stamps)
    provider = oanda(server)
    start = stamps[3000]
    result = await download_backward(provider, store, "XAU_USD", start)
    await provider.aclose()
    assert await store.timestamps("oanda", "XAU_USD") == stamps[3000:]
    assert result.complete and len(server.requests) == 2
    assert await store.download_marks("oanda", "XAU_USD") == (None, start)


# A partly downloaded history

START, END = utc(2024, 10, 18, 21), utc(2024, 11, 22, 22)
M1 = synthetic_m1(START, END, METAL_HOURS)


def shifted(c: Candle, by: float) -> Candle:
    move = lambda p: OHLC(*(x + by for x in p))  # noqa: E731
    return Candle(c.ts, move(c.bid), move(c.ask), move(c.mid), c.volume + 1000)


# H1 deliberately unlike M1 (other prices and volumes), so a bar built from the wrong
# one, or a stale cached bar, shows.
H1 = [shifted(c, 5.0) for c in aggregate_candles(M1, 3600)]


class Interrupted(Exception):
    pass


class Flaky(FakeProvider):
    """Fails on the `fail_at`-th M1 page."""

    def __init__(self, *args, fail_at: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_at = fail_at
        self.m1_pages = 0

    async def fetch_page_before(self, instrument, end, granularity="M1"):
        if granularity == "M1":
            self.m1_pages += 1
            if self.m1_pages == self.fail_at:
                raise Interrupted("connection lost")
        return await super().fetch_page_before(instrument, end, granularity)


async def check(store, cache):
    """The invariants after every page of a download."""
    stored = await store.timestamps("fake", "XAU_USD")
    # One block ending at the present: exactly the newest minutes, nothing missing.
    assert stored == [c.ts for c in M1[len(M1) - len(stored) :]]
    # Gaps and repair see no hole: the part not downloaded yet is not a gap.
    assert find_gaps(stored, METAL_HOURS, 2) == []
    # The cache equals what an uncached request computes, H1 before the boundary hour
    # and M1 from there, including the bar the boundary falls in.
    await cache.extend_all("fake", "XAU_USD")
    boundary = await store.h1_boundary("fake", "XAU_USD")
    for name in ("4h", "1D", "1W"):
        tf = TIMEFRAMES[name]
        frame = await store.read_base_frame("fake", "XAU_USD", 0, END, None)
        expected = resample_full(frame, tf)
        expected = expected[expected["ts"] < END - tf.seconds].reset_index(drop=True)
        cached = await full_cache(store, name)
        cached = cached[cached["ts"] < END - tf.seconds].reset_index(drop=True)
        pd.testing.assert_frame_equal(cached, expected)
        assert await store.cache_dirty("fake", "XAU_USD", name) == []
    return stored, boundary


async def test_download_newest_first_is_contiguous_after_every_page_and_resumes(store):
    provider = Flaky({"XAU_USD": M1}, h1={"XAU_USD": H1}, fail_at=6)
    provider.page_size = 2000  # about 15 pages of M1
    cache = CandleCache(store, Clock(END))

    # H1 first, everything: 1h and higher charts are complete from here on.
    await deep_fill(provider, store, "XAU_USD")
    assert await store.timestamps("fake", "XAU_USD", granularity="H1") == [c.ts for c in H1]
    await cache.extend_all("fake", "XAU_USD")

    # Then M1 newest first, checked after every page, until the connection drops.
    boundaries = []
    with pytest.raises(Interrupted):
        while True:
            result = await download_backward(provider, store, "XAU_USD", START, max_pages=1)
            stored, boundary = await check(store, cache)
            assert not result.complete and result.oldest == stored[0]
            assert not boundaries or boundary < boundaries[-1]  # moving back each page
            boundaries.append(boundary)
    assert len(boundaries) == 5 and 0 < await store.count("fake", "XAU_USD") < len(M1)
    assert await store.download_marks("fake", "XAU_USD") == (H1[0].ts, None)  # M1 unfinished

    # A new run continues backward from the oldest stored candle, to the end.
    provider.fail_at = None
    result = await download_backward(provider, store, "XAU_USD", START)
    assert result.complete
    assert await store.timestamps("fake", "XAU_USD") == [c.ts for c in M1]
    # The provider had nothing older than its first minute (a weekend before START).
    assert await store.download_marks("fake", "XAU_USD") == (H1[0].ts, M1[0].ts)
    await check(store, cache)
    # Every bar is from M1 now: no H1 prices (5 higher) are left in the cache.
    m1_only = await store.read_base_frame("fake", "XAU_USD", 0, END, None, use_h1=False)
    reference = resample_full(m1_only, TIMEFRAMES["1D"])
    days = await full_cache(store, "1D")
    complete = days["ts"] < END - 86400
    pd.testing.assert_frame_equal(
        days[complete].reset_index(drop=True),
        reference[reference["ts"] < END - 86400].reset_index(drop=True),
    )


async def test_repair_and_closures_leave_the_part_not_downloaded_alone(store):
    recent = [c for c in M1 if c.ts >= utc(2024, 11, 18)]
    provider = FakeProvider({"XAU_USD": M1})
    await store.upsert("fake", "XAU_USD", recent)
    result = await repair(provider, store, "XAU_USD", METAL_HOURS)
    assert result.holes == [] and result.requests == 0  # nothing before is requested
    assert not await learn_closures(store, "fake", "XAU_USD", METAL_HOURS)
    assert await store.closures("fake", "XAU_USD") == []


async def test_the_closure_scan_waits_for_the_download(store):
    provider = FakeProvider({"XAU_USD": M1})
    feed = Feed(provider, store, ["XAU_USD"], hours={"XAU_USD": METAL_HOURS})
    await store.upsert("fake", "XAU_USD", [c for c in M1 if c.ts >= utc(2024, 11, 18)])
    await feed.find_downloads()
    assert feed.downloading == {"XAU_USD"}
    await feed.scan_closures("XAU_USD")
    assert not await store.closures_scanned("fake", "XAU_USD")


@pytest.mark.parametrize("name", ["4h", "1D", "1W"])
async def test_the_bar_holding_the_h1_m1_boundary_takes_each_part_once(store, name):
    """M1 starting at 15:37 UTC on a weekday: the bar holding the boundary has H1 up to
    15:00 (H1's 15:00 candle overlaps M1 and is not used) and M1 from 16:00 on."""
    tf = TIMEFRAMES[name]
    first_m1 = utc(2024, 11, 13, 15, 37)
    await store.upsert("fake", "XAU_USD", H1, "H1")
    await store.upsert("fake", "XAU_USD", [c for c in M1 if c.ts >= first_m1])
    boundary = await store.h1_boundary("fake", "XAU_USD")
    assert boundary == utc(2024, 11, 13, 16)
    cache = CandleCache(store, Clock(END))
    await cache.extend_all("fake", "XAU_USD")
    cached = await full_cache(store, name)
    start, end = bar_bounds(boundary, tf)
    bar = cached[cached["ts"] == start].iloc[0]

    h1 = [c for c in H1 if start <= c.ts < boundary]
    m1 = [c for c in M1 if boundary <= c.ts < end]
    assert h1 and m1
    assert bar["mid_o"] == h1[0].mid.o and bar["mid_c"] == m1[-1].mid.c
    assert bar["mid_h"] == max(c.mid.h for c in h1 + m1)
    assert bar["v"] == sum(c.volume for c in h1 + m1)  # nothing counted twice


async def seed_two(path):
    async with await Database.open(path / "openchart.db") as db:
        s = CandleStore(db.conn)
        for info in (GOLD, InstrumentInfo("EUR_USD", "EUR_USD", "EUR/USD", 5, 0.0001, "CURRENCY")):
            await s.upsert_instrument("oanda", info, METAL_HOURS)
            await s.upsert("oanda", info.instrument, M1[:500])
            await s.upsert("oanda", info.instrument, H1[:50], "H1")
            await s.replace_closures("oanda", info.instrument, 0, 2**40, [(1, 2)])
        await db.conn.execute(
            "INSERT INTO drawings (id, instrument, type, points_json, style_json, created_at,"
            " updated_at) VALUES ('d1', 'EUR_USD', 'hline', '[]', '{}', 0, 0)"
        )
        await db.conn.commit()
    (path / "ticks" / "oanda" / "EUR_USD" / "date=2024-11-01").mkdir(parents=True)


def test_purge_deletes_one_instrument_and_leaves_the_others(tmp_path, monkeypatch, capsys):
    import asyncio

    from openchart import main as cli

    asyncio.run(seed_two(tmp_path))
    monkeypatch.setenv("OPENCHART_DATA_DIR", str(tmp_path))
    assert cli.main(["purge", "eur_usd"]) == 0  # only shows
    out = capsys.readouterr().out
    assert "Nothing deleted" in out and "1 drawing(s) and 0 alert(s): kept" in out
    assert cli.main(["purge", "EUR_USD", "--yes"]) == 0

    async def left():
        async with await Database.open(tmp_path / "openchart.db") as db:
            s = CandleStore(db.conn)
            names = [r.instrument for r in await s.instruments()]
            counts = {
                name: (await s.count("oanda", name), await s.count("oanda", name, "H1"))
                for name in ("XAU_USD", "EUR_USD")
            }
            closures = await s.closures("oanda", "XAU_USD"), await s.closures("oanda", "EUR_USD")
            drawings = await db.conn.execute_fetchall("SELECT id FROM drawings")
            return names, counts, closures, drawings

    names, counts, closures, drawings = asyncio.run(left())
    assert names == ["XAU_USD"]
    assert counts == {"XAU_USD": (500, 50), "EUR_USD": (0, 0)}
    assert closures == ([(1, 2)], [])
    assert [tuple(r) for r in drawings] == [("d1",)]  # kept without --drawings-and-alerts
    assert not (tmp_path / "ticks" / "oanda" / "EUR_USD").exists()

    lock = DataFolderLock(tmp_path)  # as a running server holds it
    assert lock.acquire()
    try:
        assert cli.main(["purge", "XAU_USD", "--yes"]) == 1  # the server must be stopped
        assert "stop it first" in capsys.readouterr().err
    finally:
        lock.release()
    assert not in_use(tmp_path)
