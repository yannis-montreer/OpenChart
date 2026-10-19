"""Backfill through the real OANDA provider against a mock that pages like OANDA does.

Regression test: backfill used to stop after 9,999 candles because a 4,999-candle page
(includeFirst=false) was taken as "reached the present".
"""

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from openchart.config import OandaConfig
from openchart.market import backfill as backfill_module
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


def world(n: int, at: float | None = None) -> tuple[list[int], datetime, CandleServer]:
    """The stored-to-be candles, an `end` half a minute into the forming candle, and a server
    whose clock is that `end`. Nothing here reads the clock again, so the result does not
    depend on the second a run starts in (see the parametrized test below)."""
    stamps = recent_stamps(n, at)
    end = datetime.fromtimestamp(stamps[-1] + 30, UTC)
    return stamps, end, CandleServer(stamps, incomplete_last=True, now=end.timestamp())


def recent_stamps(n: int, at: float | None = None) -> list[int]:
    now = int(time.time() if at is None else at) // 60 * 60
    return [now - 60 * (n - 1 - i) for i in range(n)]  # last one is the forming candle


async def single_run_scenario(store, at: float | None = None) -> None:
    stamps, end, server = world(N, at)
    provider = make_provider(server)
    start = datetime.fromtimestamp(stamps[0], UTC)
    events: list[tuple[int, int, str]] = []

    result = await backfill(
        provider, store, "XAU_USD", start, end, progress=lambda *e: events.append(e)
    )

    assert len(server.requests) > 3
    assert all(int(r.url.params["count"]) == MAX_CANDLES_PER_REQUEST for r in server.requests)
    assert await store.count("oanda", "XAU_USD") == N - 1  # all complete candles
    assert result.last_ts == stamps[-2]  # the latest complete candle
    # Progress only moves forward and ends on the latest complete candle.
    forward = [ts for _, ts, d in events if d == "forward"]
    assert forward == sorted(forward) and forward[-1] == stamps[-2]

    server.requests.clear()
    events.clear()
    again = await backfill(
        provider, store, "XAU_USD", start, end, progress=lambda *e: events.append(e)
    )
    await provider.aclose()
    assert again.written == 0
    assert await store.count("oanda", "XAU_USD") == N - 1
    assert len(server.requests) == 1  # one request confirms nothing new
    assert events == []


async def test_single_run_reaches_latest_complete_candle_then_rerun_adds_nothing(store):
    await single_run_scenario(store)


# The test clock (tests/clock.py) starts on an exact minute, so a run early in the process,
# or a real clock at hh:mm:00, is in the first second of the forming candle's minute. These
# moments must give the same result as any other second.
@pytest.mark.parametrize("second", [0.0, 0.2, 0.9, 1.0, 30.0, 59.9])
async def test_the_result_does_not_depend_on_the_second_of_the_minute(store, monkeypatch, second):
    at = 1_791_990_000 + second  # a minute boundary plus `second`
    monkeypatch.setattr(time, "time", lambda: at)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(at, tz)

    monkeypatch.setattr(backfill_module, "datetime", Frozen)
    await single_run_scenario(store, at)


async def test_earlier_start_is_reported_as_searching_back(store):
    stamps, end, server = world(N)
    provider = make_provider(server)
    first = datetime.fromtimestamp(stamps[0], UTC)
    await backfill(provider, store, "XAU_USD", first, end)

    # A later run whose start falls before the first stored candle (as when
    # "2 years ago" lands on a weekend) searches backwards. Its progress must not
    # look like the forward position going back in time.
    earlier = first - timedelta(days=1)
    events: list[tuple[int, int, str]] = []
    result = await backfill(
        provider, store, "XAU_USD", earlier, end, progress=lambda *e: events.append(e)
    )
    await provider.aclose()
    assert result.written == 0
    assert events and all(d == "backward" for _, _, d in events)
    assert events[-1][1] == int(earlier.timestamp())
