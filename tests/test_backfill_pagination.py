"""Backfill through the real OANDA provider against a mock that pages like OANDA does.

Regression test: backfill used to stop after 9,999 candles because a 4,999-candle page
(includeFirst=false) was taken as "reached the present".
"""

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from openchart.config import OandaConfig
from openchart.market.backfill import backfill
from openchart.providers.oanda import MAX_CANDLES_PER_REQUEST, OandaProvider
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.oanda_mock import TOKEN, CandleServer

N = 20_000  # 5 pages: 5000 + 4 x 4999 > 20,000 - 1


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield CandleStore(db.conn)


def make_provider(server: CandleServer) -> OandaProvider:
    config = OandaConfig("practice", TOKEN, "101-001-1-001")
    return OandaProvider(config, transport=httpx.MockTransport(server), request_delay=0)


def recent_stamps(n: int) -> list[int]:
    now = int(time.time()) // 60 * 60
    return [now - 60 * (n - 1 - i) for i in range(n)]  # last one is the forming candle


async def test_single_run_reaches_latest_complete_candle_then_rerun_adds_nothing(store):
    stamps = recent_stamps(N)
    server = CandleServer(stamps, incomplete_last=True)
    provider = make_provider(server)
    start = datetime.fromtimestamp(stamps[0], UTC)
    events: list[tuple[int, int, str]] = []

    result = await backfill(provider, store, "XAU_USD", start, progress=lambda *e: events.append(e))

    assert len(server.requests) > 3
    assert all(int(r.url.params["count"]) == MAX_CANDLES_PER_REQUEST for r in server.requests)
    assert await store.count("oanda", "XAU_USD") == N - 1  # all complete candles
    assert result.last_ts == stamps[-2]  # the latest complete candle
    # Progress only moves forward and ends on the latest complete candle.
    forward = [ts for _, ts, d in events if d == "forward"]
    assert forward == sorted(forward) and forward[-1] == stamps[-2]

    server.requests.clear()
    events.clear()
    again = await backfill(provider, store, "XAU_USD", start, progress=lambda *e: events.append(e))
    await provider.aclose()
    assert again.written == 0
    assert await store.count("oanda", "XAU_USD") == N - 1
    assert len(server.requests) == 1  # one request confirms nothing new
    assert events == []


async def test_earlier_start_is_reported_as_searching_back(store):
    stamps = recent_stamps(N)
    server = CandleServer(stamps, incomplete_last=True)
    provider = make_provider(server)
    first = datetime.fromtimestamp(stamps[0], UTC)
    await backfill(provider, store, "XAU_USD", first)

    # A later run whose start falls before the first stored candle (as when
    # "2 years ago" lands on a weekend) searches backwards. Its progress must not
    # look like the forward position going back in time.
    earlier = first - timedelta(days=1)
    events: list[tuple[int, int, str]] = []
    result = await backfill(
        provider, store, "XAU_USD", earlier, progress=lambda *e: events.append(e)
    )
    await provider.aclose()
    assert result.written == 0
    assert events and all(d == "backward" for _, _, d in events)
    assert events[-1][1] == int(earlier.timestamp())
