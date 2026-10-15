"""OANDA's parameter rules, enforced by the mock (tests/oanda_mock.py), and the checks of
PR #11 (the hour before a stop, the previous day) run through the real OANDA client
against it. Also: errors that explain themselves, and a broken token caught before any
request.

The owner's one-request check (2026-10-15) got an empty HTTP 400 from OANDA: the token had
arrived as a single control character (Ctrl+V typed into Read-Host), which the front of
the API refuses without a word. The request itself was well formed.
"""

import time
from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest

from openchart.config import OandaConfig
from openchart.market.backfill import check_day, check_tail
from openchart.market.feed import Feed
from openchart.providers.base import OHLC, AuthError, RequestError
from openchart.providers.oanda import OandaProvider, parse_candle, token_problem
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.oanda_mock import TOKEN, CandleServer, raw_candle

GOLD = "XAU_USD"


def provider_for(server: CandleServer, token: str = TOKEN) -> OandaProvider:
    config = OandaConfig(environment="practice", token=token, account_id="101-001-0000000-001")
    return OandaProvider(
        config, transport=httpx.MockTransport(server), max_retries=0, request_delay=0
    )


def utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


async def ask(params: dict, token: str = TOKEN) -> httpx.Response:
    server = CandleServer([utc(2024, 3, 5)])
    headers = {"Authorization": f"Bearer {token}", "Accept-Datetime-Format": "UNIX"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        return await client.get(
            "https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles",
            params={"granularity": "M1", "price": "MBA", **params},
            headers=headers,
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"from": utc(2024, 3, 5), "to": utc(2024, 3, 6), "count": 10}, "together"),
        ({"count": 5001}, "Maximum value for 'count'"),
        ({"count": 0}, "'count'"),
        ({"from": "2024-03-05T00:00:00Z", "count": 10}, "'from'"),
        ({"from": time.time() + 3600, "count": 10}, "in the future"),
        ({"to": time.time() + 3600, "count": 10}, "in the future"),
        ({"count": 10, "price": "X"}, "'price'"),
        ({"count": 10, "granularity": "M2"}, "'granularity'"),
    ],
)
async def test_the_mock_refuses_what_oanda_refuses(params, message):
    response = await ask(params)
    assert response.status_code == 400
    assert message in response.json()["errorMessage"]


async def test_the_mock_accepts_what_our_client_sends():
    assert (await ask({"from": utc(2024, 3, 5), "count": 5000})).status_code == 200
    assert (await ask({"to": utc(2024, 3, 5), "count": 5000})).status_code == 200
    assert (await ask({"count": 5000})).status_code == 200


async def test_tokens_the_api_cannot_read_get_an_empty_400():
    response = await ask({"count": 10}, token="\x16")  # Ctrl+V typed as a character
    assert response.status_code == 400 and response.content == b""
    assert (await ask({"count": 10}, token="wrong-token")).status_code == 401


# Errors that explain themselves


async def test_a_refused_request_says_why_and_what_was_asked():
    provider = provider_for(CandleServer([utc(2024, 3, 5)]))
    with pytest.raises(RequestError) as raised:
        await provider._get("/v3/instruments/XAU_USD/candles", {"count": 5001})
    text = str(raised.value)
    assert "OANDA HTTP 400 for /v3/instruments/XAU_USD/candles?count=5001" in text
    assert "Maximum value for 'count' exceeded" in text
    assert TOKEN not in text and "Bearer" not in text
    await provider.aclose()


async def test_an_empty_refusal_is_described():
    def empty(request):
        return httpx.Response(400, content=b"")

    config = OandaConfig(environment="practice", token=TOKEN, account_id="x")
    provider = OandaProvider(config, transport=httpx.MockTransport(empty), max_retries=0)
    with pytest.raises(RequestError, match="empty response body"):
        await provider._get("/v3/instruments/XAU_USD/candles", {"count": 1})
    await provider.aclose()


@pytest.mark.parametrize("token", ["\x16", "abc\n", "two words", ""])
async def test_a_token_that_cannot_be_one_is_refused_before_any_request(token):
    server = CandleServer([utc(2024, 3, 5)])
    provider = provider_for(server, token=token) if token else None
    if provider is None:  # an empty token is "not configured" already
        assert token_problem(token) == "the OANDA token is empty"
        return
    with pytest.raises(AuthError, match="Enter it again"):
        await provider._get("/v3/instruments/XAU_USD/candles", {"count": 1})
    with pytest.raises(AuthError):
        async for _ in provider.stream_ticks([GOLD]):
            pass
    assert server.requests == []  # nothing was sent
    assert token not in str(token_problem(token)) or not token.strip()
    await provider.aclose()


def test_a_real_token_shape_passes():
    assert (
        token_problem("0123456789abcdef0123456789abcdef-0123456789abcdef0123456789abcdef") is None
    )


# The checks of PR #11 through the real client


def tick_built(candle):
    m = candle.mid
    return replace(candle, mid=OHLC(m.o, m.h - 0.05, m.l, m.c), volume=candle.volume - 3)


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        yield CandleStore(db.conn)


async def test_the_daily_check_is_one_accepted_request(store):
    stamps = list(range(utc(2024, 3, 5), utc(2024, 3, 7), 60))
    broker = [parse_candle(raw_candle(t)) for t in stamps]
    tuesday = broker[:1440]
    local = [tick_built(c) if i in (10, 1080) else c for i, c in enumerate(tuesday)]
    await store.upsert_m1("oanda", GOLD, local)
    server = CandleServer(stamps)
    provider = provider_for(server)

    result = await check_day(provider, store, GOLD, date(2024, 3, 5))

    assert server.refused == [] and len(server.requests) == 1
    assert dict(server.requests[0].url.params) == {
        "granularity": "M1",
        "price": "MBA",
        "from": str(utc(2024, 3, 5)),
        "count": "5000",
    }
    assert (result.fetched, result.replaced, result.added) == (1440, 2, 0)
    assert await store.read_m1("oanda", GOLD, utc(2024, 3, 5), utc(2024, 3, 6)) == tuesday
    await provider.aclose()


async def test_the_start_up_check_is_accepted(store):
    now = int(time.time()) // 60 * 60
    stamps = list(range(now - 3 * 3600, now, 60))
    broker = [parse_candle(raw_candle(t)) for t in stamps]
    stop = now - 3600
    before_stop = [c for c in broker if c.ts < stop]
    await store.upsert_m1(
        "oanda", GOLD, [*before_stop[:-2], *(tick_built(c) for c in before_stop[-2:])]
    )
    server = CandleServer(stamps)
    provider = provider_for(server)

    await Feed(provider, store, [GOLD], hours={GOLD: None}).fill_history()

    assert server.refused == []
    for c in before_stop[-2:]:
        [stored] = await store.read_m1("oanda", GOLD, c.ts, c.ts + 60)
        assert stored == c
    assert await store.count("oanda", GOLD) == len(broker)
    await provider.aclose()


async def test_the_wake_check_is_accepted(store):
    now = int(time.time()) // 60 * 60
    stamps = list(range(now - 2 * 3600, now, 60))
    broker = [parse_candle(raw_candle(t)) for t in stamps]
    await store.upsert_m1("oanda", GOLD, [*broker[:-5], tick_built(broker[-5]), *broker[-4:]])
    server = CandleServer(stamps)
    provider = provider_for(server)
    result = await check_tail(provider, store, GOLD, before=broker[-1].ts + 60)
    assert server.refused == [] and result.replaced == 1
    await provider.aclose()
