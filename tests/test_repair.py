"""Holes inside stored M1 history: found and refilled (startup, reconnect, wake-up, CLI)."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from openchart import main as cli
from openchart.config import Config
from openchart.market import feed as feed_module
from openchart.market.backfill import backfill, gap_fill, refresh_recent, repair
from openchart.market.feed import Feed
from openchart.market.gaps import find_gaps
from openchart.market.instruments import METAL_HOURS
from openchart.providers.fake import FakeProvider, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.conftest import GOLD

START = datetime(2024, 3, 4, tzinfo=UTC)  # Monday
END = datetime(2024, 3, 16, tzinfo=UTC)
CANDLES = synthetic_m1(int(START.timestamp()), int(END.timestamp()), METAL_HOURS)


def utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


@pytest.fixture
async def db(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


async def delete(db: Database, start: int, end: int) -> int:
    cur = await db.conn.execute("DELETE FROM candles_m1 WHERE ts>=? AND ts<?", (start, end))
    await db.conn.commit()
    return cur.rowcount


async def test_a_hole_in_the_middle_is_refilled(db):
    store = CandleStore(db.conn)
    provider = FakeProvider({"XAU_USD": CANDLES})
    await backfill(provider, store, "XAU_USD", START, END)
    removed = await delete(db, utc(2024, 3, 6, 2, 38), utc(2024, 3, 6, 7, 50))
    assert removed > 300
    assert len(find_gaps(await store.timestamps("fake", "XAU_USD"), METAL_HOURS)) == 1

    result = await repair(provider, store, "XAU_USD", METAL_HOURS)
    assert len(result.holes) == 1 and result.filled == removed and result.requests == 1
    assert await store.count("fake", "XAU_USD") == len(CANDLES)
    assert find_gaps(await store.timestamps("fake", "XAU_USD"), METAL_HOURS) == []

    # Nothing left to do: no request at all.
    again = await repair(provider, store, "XAU_USD", METAL_HOURS)
    assert again.holes == [] and again.requests == 0


async def test_market_closures_are_not_holes(db):
    store = CandleStore(db.conn)
    provider = FakeProvider({"XAU_USD": CANDLES})
    await backfill(provider, store, "XAU_USD", START, END)
    result = await repair(provider, store, "XAU_USD", METAL_HOURS, min_open_minutes=1)
    assert result.holes == [] and provider.fetch_calls == 1  # the backfill's


async def test_a_hole_the_broker_has_no_data_for_stays_and_changes_nothing(db):
    store = CandleStore(db.conn)
    holiday = (utc(2024, 3, 7, 12), utc(2024, 3, 7, 18))
    broker = [c for c in CANDLES if not holiday[0] <= c.ts < holiday[1]]
    provider = FakeProvider({"XAU_USD": broker})
    await backfill(provider, store, "XAU_USD", START, END)
    before = await store.count("fake", "XAU_USD")
    epoch = store.epoch("fake", "XAU_USD")

    result = await repair(provider, store, "XAU_USD", METAL_HOURS)
    assert len(result.holes) == 1 and result.filled == 0
    assert await store.count("fake", "XAU_USD") == before
    assert store.epoch("fake", "XAU_USD") == epoch  # nothing written, cache untouched


async def test_stored_candles_are_never_replaced(db):
    store = CandleStore(db.conn)
    await backfill(FakeProvider({"XAU_USD": CANDLES}), store, "XAU_USD", START, END)
    await delete(db, utc(2024, 3, 6, 3), utc(2024, 3, 6, 4))
    edited = utc(2024, 3, 6, 2, 59)  # next to the hole, inside the refetched range
    await db.conn.execute("UPDATE candles_m1 SET mid_c=1.0 WHERE ts=?", (edited,))
    await db.conn.commit()

    await repair(FakeProvider({"XAU_USD": CANDLES}), store, "XAU_USD", METAL_HOURS)
    [candle] = await store.read_m1("fake", "XAU_USD", edited, edited + 60)
    assert candle.mid.c == 1.0


async def test_nearby_holes_share_one_request_far_ones_do_not(db):
    store = CandleStore(db.conn)
    provider = FakeProvider({"XAU_USD": CANDLES})
    await backfill(provider, store, "XAU_USD", START, END)
    for start in (utc(2024, 3, 5, 3), utc(2024, 3, 5, 9), utc(2024, 3, 13, 3)):
        await delete(db, start, start + 1800)
    provider.fetch_calls = 0

    result = await repair(provider, store, "XAU_USD", METAL_HOURS)
    assert len(result.holes) == 3 and result.requests == 2 == provider.fetch_calls
    assert await store.count("fake", "XAU_USD") == len(CANDLES)


async def test_a_hole_reaching_into_the_range_is_found(db):
    store = CandleStore(db.conn)
    provider = FakeProvider({"XAU_USD": CANDLES})
    await backfill(provider, store, "XAU_USD", START, END)
    await delete(db, utc(2024, 3, 6, 1), utc(2024, 3, 6, 5))
    result = await repair(provider, store, "XAU_USD", METAL_HOURS, start=utc(2024, 3, 6, 3))
    assert len(result.holes) == 1 and result.filled == 240


# The real-world case: the computer slept for two hours, on waking the server's
# once-a-minute refresh stored the last 15 minutes, and gap-fill only looks at the tail.


def recent(hours_back: float = 72) -> tuple[int, list]:
    now = int(time.time()) // 60 * 60
    return now, synthetic_m1(now - int(hours_back * 3600), now)  # no hours: every minute


async def sleep_through(store: CandleStore, provider: FakeProvider, now: int) -> None:
    await backfill(provider, store, "XAU_USD", datetime.fromtimestamp(now - 72 * 3600, UTC))
    await delete_after(store, now - 2 * 3600)  # asleep for the last two hours
    await refresh_recent(provider, store, "XAU_USD")  # the first refresh after waking


async def delete_after(store: CandleStore, ts: int) -> None:
    await store.conn.execute("DELETE FROM candles_m1 WHERE ts>=?", (ts,))
    await store.conn.commit()


async def test_the_sleep_hole_is_invisible_to_gap_fill(db):
    store = CandleStore(db.conn)
    now, candles = recent()
    provider = FakeProvider({"XAU_USD": candles})
    await sleep_through(store, provider, now)

    assert (await gap_fill(provider, store, "XAU_USD")).written <= 1
    [hole] = find_gaps(await store.timestamps("fake", "XAU_USD"), None)
    assert hole.open_minutes > 90


async def test_restart_repairs_the_hole(db):
    store = CandleStore(db.conn)
    now, candles = recent()
    provider = FakeProvider({"XAU_USD": candles})
    await sleep_through(store, provider, now)

    # What a server start (and every stream reconnect) runs.
    await Feed(provider, store, ["XAU_USD"], hours={"XAU_USD": None}).fill_history()
    assert find_gaps(await store.timestamps("fake", "XAU_USD"), None) == []
    assert await store.count("fake", "XAU_USD") == len(candles)


async def test_old_holes_are_left_to_the_cli_on_start(db):
    store = CandleStore(db.conn)
    now, candles = recent(hours_back=24 * 10)
    provider = FakeProvider({"XAU_USD": candles})
    await backfill(provider, store, "XAU_USD", datetime.fromtimestamp(candles[0].ts, UTC))
    await delete(db, now - 8 * 86400, now - 8 * 86400 + 3600)  # a week old
    await Feed(provider, store, ["XAU_USD"], hours={"XAU_USD": None}).fill_history()
    assert len(find_gaps(await store.timestamps("fake", "XAU_USD"), None)) == 1


async def test_waking_up_repairs_without_a_reconnect(db, monkeypatch):
    store = CandleStore(db.conn)
    now, candles = recent()
    provider = FakeProvider({"XAU_USD": candles})
    await sleep_through(store, provider, now)

    feed = Feed(provider, store, ["XAU_USD"], hours={"XAU_USD": None}, refresh_interval=1e9)
    real = time.time
    jumps = iter([0.0, 7200.0])  # the loop's first second lasts two hours
    monkeypatch.setattr(feed_module.time, "time", lambda: real() + next(jumps, 7200.0))
    task = asyncio.create_task(feed._periodic())
    try:
        async with asyncio.timeout(5):
            while find_gaps(await store.timestamps("fake", "XAU_USD"), None):  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await store.count("fake", "XAU_USD") == len(candles)


async def test_cli_repair(tmp_path, monkeypatch, capsys):
    config = replace(Config(), data_dir=tmp_path)
    provider = FakeProvider({"XAU_USD": CANDLES})
    async with await Database.open(config.db_path) as db:
        store = CandleStore(db.conn)
        await store.upsert_instrument("fake", GOLD, METAL_HOURS)
        await backfill(provider, store, "XAU_USD", START, END)
        await delete(db, utc(2024, 3, 6, 2, 38), utc(2024, 3, 6, 7, 50))

    async def saved_config(config, db):
        return config

    monkeypatch.setattr(cli, "_saved_config", saved_config)
    monkeypatch.setattr(cli, "create_provider", lambda config: provider)
    args = cli.build_parser().parse_args(["repair", "XAU_USD"])
    assert await cli._cmd_repair(config, args) == 0
    out = capsys.readouterr().out
    assert "2024-03-06 02:37 UTC -> 2024-03-06 07:50 UTC" in out and "filled" in out
    assert "1 hole(s)" in out

    async with await Database.open(config.db_path) as db:
        assert await CandleStore(db.conn).count("fake", "XAU_USD") == len(CANDLES)
