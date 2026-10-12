from datetime import UTC, datetime, timedelta

import pytest

from openchart.market.backfill import backfill, gap_fill
from openchart.market.gaps import find_gaps
from openchart.market.instruments import METAL_HOURS
from openchart.providers.fake import FakeProvider, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

START = datetime(2024, 3, 1, tzinfo=UTC)  # Friday
END = datetime(2024, 3, 15, tzinfo=UTC)
CANDLES = synthetic_m1(int(START.timestamp()), int(END.timestamp()), METAL_HOURS)


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield CandleStore(db.conn)


async def test_backfill_is_idempotent(store):
    provider = FakeProvider({"XAU_USD": CANDLES})
    first = await backfill(provider, store, "XAU_USD", START, END)
    assert first.written == len(CANDLES)
    assert await store.count("fake", "XAU_USD") == len(CANDLES)

    second = await backfill(provider, store, "XAU_USD", START, END)
    assert second.written == 0
    assert await store.count("fake", "XAU_USD") == len(CANDLES)


async def test_stored_history_has_no_gaps_beyond_closures(store):
    await backfill(FakeProvider({"XAU_USD": CANDLES}), store, "XAU_USD", START, END)
    stamps = await store.timestamps("fake", "XAU_USD")
    assert find_gaps(stamps, METAL_HOURS, min_open_minutes=1) == []


class FailingProvider(FakeProvider):
    """Drops the connection after `fail_after` candles."""

    def __init__(self, candles, fail_after: int):
        super().__init__({"XAU_USD": candles})
        self.fail_after = fail_after

    async def fetch_candles(self, instrument, start, end, granularity="M1"):
        n = 0
        async for c in super().fetch_candles(instrument, start, end, granularity):
            if n == self.fail_after:
                raise ConnectionError("network cut")
            n += 1
            yield c


async def test_interrupted_backfill_resumes(store):
    with pytest.raises(ConnectionError):
        await backfill(
            FailingProvider(CANDLES, 7000), store, "XAU_USD", START, END, batch_size=1000
        )
    assert await store.count("fake", "XAU_USD") == 7000  # committed batches survive

    provider = FakeProvider({"XAU_USD": CANDLES})
    result = await backfill(provider, store, "XAU_USD", START, END)
    assert result.written == len(CANDLES) - 7000
    assert await store.count("fake", "XAU_USD") == len(CANDLES)


async def test_extending_history_backwards_fills_head(store):
    provider = FakeProvider({"XAU_USD": CANDLES})
    middle = START + timedelta(days=7)
    await backfill(provider, store, "XAU_USD", middle, END)
    first_ts = await store.first_ts("fake", "XAU_USD")
    assert first_ts >= middle.timestamp()

    result = await backfill(provider, store, "XAU_USD", START, END, head_window=timedelta(days=2))
    assert result.first_ts == CANDLES[0].ts
    assert await store.count("fake", "XAU_USD") == len(CANDLES)
    assert find_gaps(await store.timestamps("fake", "XAU_USD"), METAL_HOURS, 1) == []


async def test_gap_fill_fetches_only_new_candles(store):
    cut = START + timedelta(days=10)
    provider = FakeProvider({"XAU_USD": CANDLES})
    await backfill(provider, store, "XAU_USD", START, cut)
    before = await store.count("fake", "XAU_USD")

    result = await gap_fill(provider, store, "XAU_USD")
    assert result.written == len(CANDLES) - before
    assert result.last_ts == CANDLES[-1].ts


async def test_gap_fill_without_history_is_noop(store):
    result = await gap_fill(FakeProvider({"XAU_USD": CANDLES}), store, "XAU_USD")
    assert result.written == 0


async def test_sources_and_instruments_are_isolated(store):
    provider = FakeProvider({"XAU_USD": CANDLES, "EUR_USD": CANDLES[:100]})
    await backfill(provider, store, "XAU_USD", START, END)
    await backfill(provider, store, "EUR_USD", START, END)
    await backfill(provider, store, "EUR_USD", START, END, source="other")
    assert await store.count("fake", "XAU_USD") == len(CANDLES)
    assert await store.count("fake", "EUR_USD") == 100
    assert await store.count("other", "EUR_USD") == 100


async def test_read_back_matches_written(store):
    await backfill(FakeProvider({"XAU_USD": CANDLES[:50]}), store, "XAU_USD", START, END)
    got = await store.read_m1("fake", "XAU_USD", 0, 2**40)
    assert got == CANDLES[:50]
