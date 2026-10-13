"""Deep H1 history, the H1/M1 switch and the 4h/D/W cache."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from openchart.market.aggregate import FULL_COLUMNS, resample_full
from openchart.market.backfill import deep_fill
from openchart.market.cache import CandleCache
from openchart.market.instruments import METAL_HOURS
from openchart.market.series import SeriesReader
from openchart.market.sessions import bar_start
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import OHLC, Candle
from openchart.providers.fake import FakeProvider, aggregate_candles, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

T = TIMEFRAMES


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


# Five weeks of gold minutes around the November 2024 DST change.
START, END = utc(2024, 10, 18, 21), utc(2024, 11, 22, 22)
M1 = synthetic_m1(START, END, METAL_HOURS)
H1 = aggregate_candles(M1, 3600)


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield CandleStore(db.conn)


async def derived(store, source, tf, start=START, end=END) -> pd.DataFrame:
    frame = await store.read_base_frame(source, "XAU_USD", start, end, None)
    return resample_full(frame, tf)


@pytest.mark.parametrize("tf", ["1h", "4h", "1D", "1W"])
async def test_bars_from_h1_equal_bars_from_m1(store, tf):
    await store.upsert("from_h1", "XAU_USD", H1, "H1")
    await store.upsert("from_m1", "XAU_USD", M1, "M1")
    a = await derived(store, "from_h1", T[tf])
    b = await derived(store, "from_m1", T[tf])
    assert len(a) > 3
    pd.testing.assert_frame_equal(a, b)


async def test_h1_before_m1_coverage_and_m1_after(store):
    switch = utc(2024, 11, 6, 13, 30)  # M1 starts mid-hour, mid-week
    await store.upsert("oanda", "XAU_USD", H1, "H1")
    await store.upsert("oanda", "XAU_USD", [c for c in M1 if c.ts >= switch], "M1")
    assert await store.h1_boundary("oanda", "XAU_USD") == utc(2024, 11, 6, 14)

    base = await store.read_base_frame("oanda", "XAU_USD", START, END, None)
    ts = base["ts"].tolist()
    assert ts == sorted(set(ts))  # no overlap between the H1 and M1 parts
    assert {t % 3600 for t in ts if t < utc(2024, 11, 6, 14)} == {0}
    # Whatever the mix, the daily bars are those of the full M1 series.
    reference = CandleStore(store.conn)
    await reference.upsert("m1_only", "XAU_USD", M1, "M1")
    pd.testing.assert_frame_equal(
        await derived(store, "oanda", T["1D"]), await derived(store, "m1_only", T["1D"])
    )


async def test_minute_timeframes_never_use_h1(store):
    await store.upsert("oanda", "XAU_USD", H1, "H1")
    reader = SeriesReader(store)
    assert (await reader.candles("oanda", "XAU_USD", T["15m"], limit=5, now=END)).empty
    hours = await reader.candles("oanda", "XAU_USD", T["1h"], limit=5, now=END)
    assert len(hours) == 5


# Cache


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


async def full_cache(store, tf) -> pd.DataFrame:
    rows = await store.conn.execute_fetchall(
        "SELECT ts, bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c,"
        " mid_o, mid_h, mid_l, mid_c, volume FROM candles_cache WHERE timeframe=? ORDER BY ts",
        (tf,),
    )
    return pd.DataFrame(rows, columns=FULL_COLUMNS).astype({"ts": "int64", "v": "int64"})


@pytest.mark.parametrize("tf", ["4h", "1D", "1W"])
async def test_cache_holds_exactly_the_complete_derived_bars(store, tf):
    await store.upsert("oanda", "XAU_USD", H1, "H1")
    await store.upsert("oanda", "XAU_USD", [c for c in M1 if c.ts >= utc(2024, 11, 6)], "M1")
    now = utc(2024, 11, 20, 15, 30)  # mid-week, mid-session
    cache = CandleCache(store, Clock(now))
    assert await cache.extend("oanda", "XAU_USD", T[tf]) > 0
    expected = await derived(store, "oanda", T[tf], end=now)
    expected = expected[expected["ts"] < bar_start(now, T[tf])]  # complete bars only
    pd.testing.assert_frame_equal(await full_cache(store, tf), expected.reset_index(drop=True))
    assert await cache.extend("oanda", "XAU_USD", T[tf]) == 0  # already up to date


async def test_cache_extends_as_bars_close(store):
    clock = Clock(utc(2024, 11, 12, 15))
    cache = CandleCache(store, clock)
    await store.upsert("oanda", "XAU_USD", [c for c in M1 if c.ts < clock.now], "M1")
    await cache.extend("oanda", "XAU_USD", T["1D"])
    before = len(await full_cache(store, "1D"))

    clock.now = utc(2024, 11, 14, 15)  # two more trading days closed
    await store.upsert("oanda", "XAU_USD", [c for c in M1 if c.ts < clock.now], "M1")
    await cache.extend("oanda", "XAU_USD", T["1D"])
    assert len(await full_cache(store, "1D")) == before + 2


async def test_writes_invalidate_affected_bars(store):
    await store.upsert("oanda", "XAU_USD", M1, "M1")
    cache = CandleCache(store, Clock(END))
    await cache.extend_all("oanda", "XAU_USD")
    days = await full_cache(store, "1D")
    target = int(days["ts"].iloc[3])

    # A corrected minute inside the 4th day: that day and later ones are dropped...
    spike = next(c for c in M1 if c.ts > target + 3 * 3600)
    big = OHLC(spike.mid.o, 9999.0, spike.mid.l, spike.mid.c)
    await store.upsert("oanda", "XAU_USD", [Candle(spike.ts, big, big, big, spike.volume)], "M1")
    remaining = await full_cache(store, "1D")
    assert remaining["ts"].max() < target
    # ...and the next extend recomputes them with the new data.
    await cache.extend("oanda", "XAU_USD", T["1D"])
    days = await full_cache(store, "1D")
    assert days.loc[days["ts"] == target, "mid_h"].iloc[0] == 9999.0


async def test_write_during_extend_is_not_cached_stale(store, monkeypatch):
    await store.upsert("oanda", "XAU_USD", M1, "M1")
    cache = CandleCache(store, Clock(END))
    real_read = store.read_base_frame
    fired = False

    async def read_then_write(*args, **kwargs):
        nonlocal fired
        frame = await real_read(*args, **kwargs)
        if not fired:  # new data lands while the cache is computing
            fired = True
            c = M1[100]
            big = OHLC(c.mid.o, 8888.0, c.mid.l, c.mid.c)
            await store.upsert("oanda", "XAU_USD", [Candle(c.ts, big, big, big, c.volume)], "M1")
        return frame

    monkeypatch.setattr(store, "read_base_frame", read_then_write)
    await cache.extend("oanda", "XAU_USD", T["1D"])
    monkeypatch.setattr(store, "read_base_frame", real_read)
    expected = await derived(store, "oanda", T["1D"])
    cached = await full_cache(store, "1D")
    assert cached["mid_h"].max() == 8888.0
    pd.testing.assert_frame_equal(cached, expected.iloc[: len(cached)].reset_index(drop=True))


async def test_reader_serves_cached_and_live_bars_like_uncached(store):
    await store.upsert("oanda", "XAU_USD", H1, "H1")
    await store.upsert("oanda", "XAU_USD", [c for c in M1 if c.ts >= utc(2024, 11, 6)], "M1")
    now = utc(2024, 11, 20, 15, 30)
    cached = SeriesReader(store, cache=CandleCache(store, Clock(now)))
    plain = SeriesReader(store)
    for tf in ("4h", "1D", "1W"):
        for kwargs in (
            {"limit": 12},
            {"limit": 5, "end": utc(2024, 11, 8)},  # scrolling left
            {"start": utc(2024, 10, 25), "limit": 7},
        ):
            a = await cached.candles("oanda", "XAU_USD", T[tf], now=now, **kwargs)
            b = await plain.candles("oanda", "XAU_USD", T[tf], now=now, **kwargs)
            pd.testing.assert_frame_equal(a, b, obj=f"{tf} {kwargs}")
    days = await cached.candles("oanda", "XAU_USD", T["1D"], now=now, limit=3)
    assert list(days["complete"]) == [True, True, False]


async def test_deep_fill_downloads_once_then_gap_fills(store):
    provider = FakeProvider(h1={"XAU_USD": H1[:-48]})
    first = await deep_fill(provider, store, "XAU_USD")
    assert first.written == len(H1) - 48
    assert first.first_ts == H1[0].ts
    provider.h1["XAU_USD"] = H1
    again = await deep_fill(provider, store, "XAU_USD")
    assert again.written == 48
    assert await store.count("fake", "XAU_USD", "H1") == len(H1)
