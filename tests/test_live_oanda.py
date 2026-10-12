"""Calls the real OANDA API. Skipped unless credentials are configured in `.env`.

Run with: pytest -m live -v
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from openchart.config import load_config
from openchart.providers.base import Heartbeat
from openchart.providers.oanda import OandaProvider

pytestmark = pytest.mark.live

CONFIG = load_config()
if not CONFIG.oanda.configured:
    pytest.skip("OANDA credentials not configured in .env", allow_module_level=True)

INSTRUMENT = "XAU_USD"


@pytest.fixture
async def provider():
    p = OandaProvider(CONFIG.oanda)
    yield p
    await p.aclose()


async def test_connection(provider):
    result = await provider.test_connection()
    assert result.ok, result.message


async def test_instrument_listed(provider):
    names = {i.instrument for i in await provider.list_instruments()}
    assert INSTRUMENT in names


async def test_fetch_one_week_of_m1(provider):
    end = datetime.now(UTC) - timedelta(days=1)
    candles = [c async for c in provider.fetch_candles(INSTRUMENT, end - timedelta(days=7), end)]
    stamps = [c.ts for c in candles]
    assert stamps == sorted(set(stamps))
    assert all(c.complete and c.ask.c >= c.bid.c for c in candles)
    # A trading week has about 5 x 23 x 60 = 6900 one-minute bars for gold.
    assert 5000 < len(candles) < 7300


async def test_stream_sends_heartbeat(provider):
    async with asyncio.timeout(20):
        async for event in provider.stream_ticks([INSTRUMENT]):
            if isinstance(event, Heartbeat):
                break
