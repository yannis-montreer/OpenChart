"""The bar cache of the read path (docs/decisions-bar-cache.md).

Two readers on one store, one with the cache and one without, must give the same answers
after every way the store's candles can be written. Each write path is run through the real
function with the fake broker, and must change at least one answer (else the comparison
would prove nothing).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from openchart.market import backfill as bf
from openchart.market.aggregate import Bar
from openchart.market.feed import Feed
from openchart.market.instruments import METAL_HOURS
from openchart.market.series import SeriesReader
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import OHLC, Candle
from openchart.providers.fake import FakeProvider, aggregate_candles, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.conftest import GOLD, NOW

try:
    from openchart.market.bars import BarCache
except ImportError:  # before the code: every test below fails, none is skipped
    BarCache = None

SRC = Path(__file__).resolve().parents[1] / "src" / "openchart"
T = TIMEFRAMES
I = "XAU_USD"  # noqa: E741
H = METAL_HOURS
DAY = 86400

# The store: deep H1 for 30 days, then M1 for 6 days up to two hours ago, with a hole of
# three hours in the middle. The broker has more of both, at other prices (base 2001), so
# every write that reaches stored bars changes them.
M1_FROM, M1_TO = NOW - 6 * DAY, NOW - 2 * 3600
H1_FROM = NOW - 30 * DAY
HOLE = (NOW - DAY - 6 * 3600, NOW - DAY - 3 * 3600)  # NOW is a Wednesday, 15:00 UTC
STORED_M1 = [c for c in synthetic_m1(M1_FROM, M1_TO, H) if not HOLE[0] <= c.ts < HOLE[1]]
STORED_H1 = aggregate_candles(synthetic_m1(H1_FROM, M1_FROM, H), 3600)
BROKER_M1 = synthetic_m1(NOW - 12 * DAY, NOW, H, base=2001.0)
BROKER_H1 = aggregate_candles(synthetic_m1(NOW - 60 * DAY, NOW, H, base=2001.0), 3600)

Q_NOW = NOW + 30  # the readers' "now": the live minute is NOW


def live_bar(instrument: str) -> Bar | None:
    m = [2000.5, 2001.5, 1999.5, 2000.7]
    return Bar(NOW, [p - 0.1 for p in m], [p + 0.1 for p in m], m, 7)


def queries():
    """(timeframe, price, start, end, limit, forming): latest windows, the 2-bar tail,
    pages forward from inside a bar, pages back (the indicator's warmup), and windows
    around the hole and the hour where M1 takes over from H1."""
    out = []
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h"):
        out += [
            (tf, "mid", None, None, 5000, True),
            (tf, "mid", None, None, 2, True),
            (tf, "mid", None, None, 300, False),
            (tf, "mid", M1_FROM - 3 * 3600 + 420, None, 400, False),
            (tf, "mid", None, HOLE[0] + 1800, 600, False),
            (tf, "mid", None, M1_FROM + 1800, 250, False),
            (tf, "mid", HOLE[0] - 7200 + 120, NOW - DAY, 5000, False),
        ]
    out += [
        ("5m", "bid", None, None, 5000, True),
        ("5m", "ask", None, HOLE[1] + 60, 1000, False),
        ("1h", "bid", H1_FROM + 7 * DAY, None, 5000, False),
    ]
    return out


async def answers(reader: SeriesReader, fixed_only=False) -> list[tuple[tuple, pd.DataFrame]]:
    out = []
    for q in queries():
        if fixed_only and q[0] == "4h":
            continue
        tf, price, start, end, limit, forming = q
        frame = await reader.candles(
            "fake", I, T[tf], price=price, start=start, end=end, limit=limit, now=Q_NOW,
            forming=forming,
        )  # fmt: skip
        out.append((q, frame))
    return out


def assert_same(got, want) -> None:
    assert len(got) == len(want)
    for (q, a), (_, b) in zip(got, want, strict=True):
        try:
            pd.testing.assert_frame_equal(a, b, check_exact=True)
        except AssertionError as exc:
            raise AssertionError(f"query {q}: {exc}") from None
        assert a.attrs == b.attrs, q


def differs(before, after) -> bool:
    return any(not a.equals(b) for (_, a), (_, b) in zip(before, after, strict=True))


@pytest.fixture
async def setup(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        await store.upsert_instrument("fake", GOLD, H)
        await store.upsert_m1("fake", I, STORED_M1)
        await store.upsert("fake", I, STORED_H1, "H1")
        cache = BarCache(store)
        cached = SeriesReader(store, bars=cache, live=live_bar)
        plain = SeriesReader(store, live=live_bar)
        provider = FakeProvider({I: BROKER_M1}, h1={I: BROKER_H1}, instruments=[GOLD])
        yield store, cache, cached, plain, provider


def candle(ts: int, price: float) -> Candle:
    p = OHLC(price, price + 1, price - 1, price + 0.5)
    return Candle(ts, OHLC(*(x - 0.1 for x in p)), OHLC(*(x + 0.1 for x in p)), p, 3)


async def feed_closed_minutes(store, provider):
    """The feed's closed minutes (CandleBuilder -> Feed.flush_m1)."""
    feed = Feed(provider, store, [I], hours={I: H})
    for k in range(3):
        feed._queue_m1(I, candle(M1_TO + 60 * k, 2003.0 + k))
    await feed.flush_m1()


async def broker_replacement(store, provider):
    """The last 15 stored minutes replaced by the broker's own (`refresh_recent`, once a
    minute; its window reaches back from the clock, here to 15 minutes before the newest
    stored minute)."""
    provider.candles[I] = [c for c in BROKER_M1 if c.ts < M1_TO]
    lookback = timedelta(seconds=time.time() - (M1_TO - 15 * 60))
    await bf.refresh_recent(provider, store, I, lookback)


async def hour_before_stop(store, provider):
    await bf.check_tail(provider, store, I)


async def previous_day(store, provider):
    day = datetime.fromtimestamp(NOW - 2 * DAY, UTC).date()
    await bf.check_day(provider, store, I, day)


async def repair_hole(store, provider):
    await bf.repair(provider, store, I, H, start=M1_FROM, end=M1_TO)


async def gap_fill(store, provider):
    await bf.gap_fill(provider, store, I)


async def backfill_both_ends(store, provider):
    """`openchart backfill`'s function: the tail after the newest minute, then the head
    before the oldest, a window per transaction."""
    start = datetime.fromtimestamp(M1_FROM - DAY, UTC)
    await bf.backfill(provider, store, I, start, datetime.fromtimestamp(NOW, UTC))


async def download_page_m1(store, provider):
    """One page of the newest-first download, before the oldest stored minute: moves the
    hour where M1 takes over from H1 back by days."""
    provider.page_size = 1500
    await bf.download_backward(provider, store, I, None, max_pages=1)


async def download_page_h1(store, provider):
    provider.page_size = 200
    await bf.download_backward(provider, store, I, None, granularity="H1", max_pages=1)


async def deep_h1(store, provider):
    """Deep H1 kept up to date: the gap fill after the newest H1, then the download."""
    await bf.deep_fill(provider, store, I)


async def purge_and_redownload(store, provider):
    await store.purge_instrument("fake", I)
    await store.upsert_instrument("fake", GOLD, H)
    await store.upsert_m1("fake", I, BROKER_M1[-3000:])


WRITE_PATHS = [
    feed_closed_minutes,
    broker_replacement,
    hour_before_stop,
    previous_day,
    repair_hole,
    gap_fill,
    backfill_both_ends,
    download_page_m1,
    download_page_h1,
    deep_h1,
    purge_and_redownload,
]


async def test_the_cache_serves_from_memory_once_primed(setup, monkeypatch):
    store, cache, cached, plain, _ = setup
    first = await answers(cached)
    assert_same(first, await answers(plain))
    reads = []
    real = store.read_frame

    async def counting(*args, **kwargs):
        reads.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "read_frame", counting)
    again = await answers(cached, fixed_only=True)
    assert_same(again, [a for a in first if a[0][0] != "4h"])
    assert reads == []  # 1m to 1h: all from memory (4h reads its tail from the store)
    assert cache.hits >= len(again)


@pytest.mark.parametrize("write", WRITE_PATHS, ids=lambda f: f.__name__)
async def test_cached_equals_uncached_after_every_write_path(setup, write):
    store, cache, cached, plain, provider = setup
    before = await answers(cached)
    await answers(cached)  # primed twice: the second round is answered from memory
    assert len(cache) > 0
    await write(store, provider)
    want = await answers(plain)
    assert differs(before, want), "this write changed no answer: the test proves nothing"
    assert_same(await answers(cached), want)
    assert_same(await answers(cached), want)  # and again, from what the cache kept


async def test_a_write_during_a_read_is_not_cached(setup, monkeypatch):
    store, _cache, cached, plain, _ = setup
    await cached.candles("fake", I, T["5m"], limit=2, now=Q_NOW)  # a run of the last 3 days
    gate, reading = asyncio.Event(), asyncio.Event()
    real = store.read_frame

    async def slow(*args, **kwargs):
        frame = await real(*args, **kwargs)
        reading.set()
        await gate.wait()  # the data is read; a write lands before the cache keeps it
        return frame

    monkeypatch.setattr(store, "read_frame", slow)
    start = M1_FROM + 3600  # before the run: the cache must read
    read = asyncio.create_task(
        cached.candles("fake", I, T["5m"], start=start, limit=5000, now=Q_NOW)
    )
    await asyncio.wait_for(reading.wait(), 10)
    monkeypatch.setattr(store, "read_frame", real)
    await store.upsert_m1("fake", I, [candle(M1_FROM + 7200, 1990.0)])
    gate.set()
    got = await asyncio.wait_for(read, 10)
    assert (got["l"] == 1989.0).any()  # read again whole after the write, as uncached
    for q in [dict(start=start, limit=5000), dict(limit=500), dict(limit=2)]:
        a = await cached.candles("fake", I, T["5m"], now=Q_NOW, **q)
        b = await plain.candles("fake", I, T["5m"], now=Q_NOW, **q)
        pd.testing.assert_frame_equal(a, b, check_exact=True)
        if "start" in q:
            assert (a["l"] == 1989.0).any()  # the minute written during the read is there


async def test_a_new_minute_keeps_the_older_bars_in_memory(setup, monkeypatch):
    """The feed writes a minute a minute: only bars from an hour before it are dropped."""
    store, _cache, cached, plain, provider = setup
    old = dict(start=M1_FROM + 3600, end=M1_TO - 3 * 3600, limit=5000, now=Q_NOW)
    await cached.candles("fake", I, T["5m"], **old)
    await feed_closed_minutes(store, provider)
    reads = []
    real = store.read_frame

    async def counting(*args, **kwargs):
        reads.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "read_frame", counting)
    got = await cached.candles("fake", I, T["5m"], **old)
    assert reads == []
    monkeypatch.setattr(store, "read_frame", real)
    pd.testing.assert_frame_equal(got, await plain.candles("fake", I, T["5m"], **old))


async def test_requests_just_past_either_end_of_a_run(setup):
    """A request that starts a few bars before a run, or ends a bar after it, reads the
    missing bars: they are not taken as cached."""
    _, _, cached, plain, _ = setup
    start, end = HOLE[0] - DAY, HOLE[0] - DAY + 6 * 3600  # a Monday, market open
    for name in ("1m", "5m", "15m"):
        tf = T[name]
        await cached.candles("fake", I, tf, start=start, end=end, limit=5000, now=Q_NOW)
        for s, e in [
            (start - 20 * 60, end),
            (start - tf.seconds, end),
            (start, end + tf.seconds),
            (start, end + 60),
            (start - tf.seconds, end + tf.seconds),
        ]:
            q = dict(start=s, end=e, limit=5000, now=Q_NOW)
            a = await cached.candles("fake", I, tf, **q)
            b = await plain.candles("fake", I, tf, **q)
            pd.testing.assert_frame_equal(
                a, b, check_exact=True, obj=f"{name} {s - start} {e - end}"
            )


async def test_changing_an_answer_does_not_change_the_cache(setup):
    _, _, cached, plain, _ = setup
    q = dict(limit=300, now=Q_NOW)
    got = await cached.candles("fake", I, T["15m"], **q)
    got.loc[0, "h"] = -1.0
    got["c"] *= 2
    pd.testing.assert_frame_equal(
        await cached.candles("fake", I, T["15m"], **q),
        await plain.candles("fake", I, T["15m"], **q),
    )


async def test_a_bump_that_did_not_tell_the_cache_drops_its_runs(setup):
    """A write path that only bumps the epoch (no oldest time) must not leave old bars."""
    store, _cache, cached, plain, _ = setup
    await answers(cached)
    await store.conn.execute(
        "UPDATE candles_m1 SET mid_c = mid_c + 1000 WHERE ts >= ?", (M1_TO - 3600,)
    )
    await store.conn.commit()
    store._epoch[("fake", I)] += 1  # what a bare bump looks like (no listener called)
    assert_same(await answers(cached), await answers(plain))


async def test_the_size_cap(setup):
    store, _, _, plain, _ = setup
    small = BarCache(store, max_bytes=40_000, max_bars=600)
    cached = SeriesReader(store, bars=small, live=live_bar)
    for _ in range(2):
        assert_same(await answers(cached), await answers(plain))
        assert small.nbytes <= 40_000
        assert all(n <= 600 for n in small.run_lengths())


async def test_only_fixed_timeframes_are_cached(setup):
    _, cache, _, _, _ = setup
    assert [n for n, tf in T.items() if cache.covers(tf)] == ["1m", "5m", "15m", "30m", "1h"]


# Every candle write goes through the store's helper that bumps the epoch before and after.

WRITE_SQL = re.compile(r"\b(INSERT|DELETE|UPDATE|REPLACE)\b|_upsert_sql\(")


def _writes_candles(source: str) -> bool:
    touches = "candles_m1" in source or "candles_h1" in source or "TABLES[" in source
    return touches and bool(WRITE_SQL.search(source))


def test_every_store_method_writing_candles_bumps_the_epoch():
    """Static: a method of the store whose SQL writes candle rows goes through `_writing`
    (or bumps the epoch itself, before and after), including ones added later."""
    code = textwrap.dedent(inspect.getsource(CandleStore))
    writers = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(code, node)
            if src and _writes_candles(src) and node.name != "_writing":
                writers.append(node.name)
                assert "self._writing(" in src or src.count("self._epoch[") >= 2, node.name
    assert {"upsert", "purge_instrument"} <= set(writers)


def test_no_other_module_names_the_candle_tables():
    allowed = {SRC / "store" / "candles.py"}
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p not in allowed and re.search(r"candles_(m1|h1)\b", p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_the_series_reader_uses_the_bar_cache():
    users = sorted(
        str(p.relative_to(SRC)).replace("\\", "/")
        for p in SRC.rglob("*.py")
        if "BarCache" in p.read_text(encoding="utf-8") or "market.bars" in p.read_text("utf-8")
    )
    assert users == ["market/bars.py", "market/series.py", "server.py"]


async def test_the_epoch_moves_before_and_after_each_candle_statement(tmp_path):
    """A statement trace: every statement writing candle rows runs after a bump, and
    another bump follows it, for each write method of the store."""
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        seen: list[tuple[str, int]] = []
        key = ("fake", I)

        def trace(sql: str) -> None:
            if re.search(r"\b(INSERT|DELETE|UPDATE)\b[^;]*\bcandles_(m1|h1)\b", sql, re.I):
                seen.append((sql[:40], store._epoch.get(key, 0)))

        await db.conn.set_trace_callback(trace)
        calls = [
            lambda: store.upsert_m1("fake", I, STORED_M1[:50]),
            lambda: store.upsert("fake", I, STORED_H1[:50], "H1"),
            lambda: store.purge_instrument("fake", I),
        ]
        for call in calls:
            seen.clear()
            before = store.epoch(*key)
            await call()
            assert seen, "no candle statement traced"
            assert all(e > before for _, e in seen)
            assert store.epoch(*key) > max(e for _, e in seen)


async def test_the_server_reads_through_the_bar_cache(client):
    server = client.server
    assert isinstance(server.reader.bars, BarCache)
    assert server.reader.bars.store is server.store
    q = {"instrument": I, "timeframe": "5m", "limit": 500}
    first = client.get("/api/candles", params=q).json()
    hits = server.reader.bars.hits
    assert client.get("/api/candles", params=q).json() == first
    assert server.reader.bars.hits > hits
    bars, server.reader.bars = server.reader.bars, None
    try:
        assert client.get("/api/candles", params=q).json() == first
    finally:
        server.reader.bars = bars
