import json
from datetime import UTC, datetime

import httpx
import pytest

from openchart.config import OandaConfig
from openchart.providers.base import AuthError, Heartbeat, RequestError, Tick
from openchart.providers.oanda import OandaProvider, parse_stream_line
from tests.oanda_mock import TOKEN, CandleServer

CONFIG = OandaConfig("practice", TOKEN, "101-001-1-001")
T0 = int(datetime(2024, 3, 4, tzinfo=UTC).timestamp())  # a Monday


def provider(handler) -> OandaProvider:
    return OandaProvider(CONFIG, transport=httpx.MockTransport(handler), request_delay=0)


def dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


async def test_paginates_and_skips_incomplete():
    stamps = [T0 + 60 * i for i in range(12_001)]
    server = CandleServer(stamps, incomplete_last=True)
    p = provider(server)
    got = [c async for c in p.fetch_candles("XAU_USD", dt(T0), dt(T0 + 10**7))]
    await p.aclose()
    assert [c.ts for c in got] == stamps[:-1]  # last one is incomplete
    assert len(server.requests) == 3
    assert "includeFirst" not in server.requests[0].url.params
    assert server.requests[1].url.params["includeFirst"] == "false"
    assert got[0].mid.c == 2000.5 and got[0].volume == 7


async def test_respects_end_bound():
    stamps = [T0 + 60 * i for i in range(100)]
    p = provider(CandleServer(stamps))
    got = [c async for c in p.fetch_candles("XAU_USD", dt(T0 + 600), dt(T0 + 1200))]
    await p.aclose()
    assert [c.ts for c in got] == list(range(T0 + 600, T0 + 1200, 60))


async def test_auth_error_is_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"errorMessage": "Insufficient authorization"})

    p = provider(handler)
    with pytest.raises(AuthError, match="Insufficient authorization"):
        [c async for c in p.fetch_candles("XAU_USD", dt(T0), dt(T0 + 60))]
    await p.aclose()
    assert calls == 1


async def test_bad_request_raises_request_error():
    p = provider(lambda r: httpx.Response(400, json={"errorMessage": "Invalid instrument"}))
    with pytest.raises(RequestError):
        [c async for c in p.fetch_candles("NOPE", dt(T0), dt(T0 + 60))]
    await p.aclose()


async def test_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("openchart.providers.oanda.asyncio.sleep", _no_sleep)
    responses = iter([httpx.Response(429), httpx.Response(503)])
    server = CandleServer([T0])

    def handler(request):
        return next(responses, None) or server(request)

    p = provider(handler)
    got = [c async for c in p.fetch_candles("XAU_USD", dt(T0), dt(T0 + 60))]
    await p.aclose()
    assert len(got) == 1


async def _no_sleep(_):
    return None


def test_parse_stream_lines():
    price = {
        "type": "PRICE",
        "instrument": "XAU_USD",
        "time": "1709510400.123456000",
        "bids": [{"price": "2050.10", "liquidity": 1}],
        "asks": [{"price": "2050.40", "liquidity": 1}],
    }
    tick = parse_stream_line(json.dumps(price))
    assert tick == Tick("XAU_USD", 1709510400.123456, 2050.10, 2050.40)
    assert parse_stream_line('{"type":"HEARTBEAT","time":"1709510405.0"}') == Heartbeat(
        1709510405.0
    )
    assert parse_stream_line("") is None
    assert parse_stream_line(json.dumps({**price, "bids": []})) is None


async def test_stream_yields_ticks_and_heartbeats():
    lines = [
        '{"type":"HEARTBEAT","time":"1709510400.0"}',
        '{"type":"PRICE","instrument":"XAU_USD","time":"1709510401.5",'
        '"bids":[{"price":"2050.1"}],"asks":[{"price":"2050.4"}]}',
    ]

    def handler(request):
        assert request.url.host == "stream-fxpractice.oanda.com"
        assert request.url.params["instruments"] == "XAU_USD,EUR_USD"
        return httpx.Response(200, content="\n".join(lines).encode())

    p = provider(handler)
    events = [e async for e in p.stream_ticks(["XAU_USD", "EUR_USD"])]
    await p.aclose()
    assert isinstance(events[0], Heartbeat)
    assert events[1] == Tick("XAU_USD", 1709510401.5, 2050.1, 2050.4)


async def test_test_connection_reports_failure_without_token():
    p = provider(lambda r: httpx.Response(401, json={"errorMessage": "bad token"}))
    result = await p.test_connection()
    await p.aclose()
    assert not result.ok
    assert TOKEN not in result.message


async def test_test_connection_ok():
    def handler(request):
        assert request.url.path == "/v3/accounts/101-001-1-001/summary"
        return httpx.Response(200, json={"account": {"id": "101-001-1-001", "currency": "USD"}})

    p = provider(handler)
    result = await p.test_connection()
    await p.aclose()
    assert result.ok and "USD" in result.message


async def test_list_instruments_metadata():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "instruments": [
                    {
                        "name": "XAU_USD",
                        "type": "METAL",
                        "displayName": "Gold",
                        "displayPrecision": 3,
                        "pipLocation": -2,
                    }
                ]
            },
        )

    p = provider(handler)
    [info] = await p.list_instruments()
    await p.aclose()
    assert (info.instrument, info.decimals, info.pip_size, info.kind) == (
        "XAU_USD",
        3,
        0.01,
        "METAL",
    )


def test_unconfigured_provider_refuses():
    with pytest.raises(AuthError):
        OandaProvider(OandaConfig())
