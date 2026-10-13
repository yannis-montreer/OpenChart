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


# Sample ranges, Friday 17:00 New York to Friday 17:00 New York, each spanning a DST change.
DST_RANGES = {
    "autumn-2025": (datetime(2025, 10, 24, 21, tzinfo=UTC), datetime(2025, 11, 14, 22, tzinfo=UTC)),
    "spring-2026": (datetime(2026, 2, 27, 22, tzinfo=UTC), datetime(2026, 3, 20, 21, tzinfo=UTC)),
}


@pytest.mark.parametrize("tf_name", ["1D", "4h"])
@pytest.mark.parametrize("period", list(DST_RANGES))
async def test_derived_candles_match_oanda(provider, tf_name, period):
    """Bars resampled from OANDA M1 equal OANDA's own D/H4 candles (SPEC section 6)."""
    import pandas as pd

    from openchart.market.aggregate import resample
    from openchart.market.timeframes import TIMEFRAMES

    tf = TIMEFRAMES[tf_name]
    start, end = DST_RANGES[period]
    m1 = [c async for c in provider.fetch_candles(INSTRUMENT, start, end)]
    broker = [c async for c in provider.fetch_candles(INSTRUMENT, start, end, tf.oanda)]
    assert len(broker) >= (14 if tf_name == "1D" else 80)

    for price in ("bid", "ask", "mid"):
        frame = pd.DataFrame(
            [(c.ts, *getattr(c, price), c.volume) for c in m1],
            columns=["ts", "o", "h", "l", "c", "v"],
        )
        derived = {row.ts: row for row in resample(frame, tf).itertuples()}
        assert set(derived) == {c.ts for c in broker}, f"{price}: bar times differ"
        for c in broker:
            d = derived[c.ts]
            assert (d.o, d.h, d.l, d.c) == pytest.approx(tuple(getattr(c, price)), abs=1e-9), (
                f"{price} {tf_name} bar at {c.ts}"
            )
            assert d.v == c.volume


@pytest.mark.parametrize("tf_name", ["1D", "4h", "1W"])
@pytest.mark.parametrize("period", list(DST_RANGES))
async def test_bars_from_oanda_h1_equal_bars_from_oanda_m1(provider, tf_name, period):
    """Deep history (H1) gives exactly the bars M1 gives, where both exist (SPEC section 6)."""
    import pandas as pd

    from openchart.market.aggregate import FULL_COLUMNS, resample_full
    from openchart.market.timeframes import TIMEFRAMES

    def frame(candles):
        return pd.DataFrame(
            [(c.ts, *c.bid, *c.ask, *c.mid, c.volume) for c in candles], columns=FULL_COLUMNS
        )

    tf = TIMEFRAMES[tf_name]
    start, end = DST_RANGES[period]
    m1 = [c async for c in provider.fetch_candles(INSTRUMENT, start, end)]
    h1 = [c async for c in provider.fetch_candles(INSTRUMENT, start, end, "H1")]
    from_m1 = resample_full(frame(m1), tf)
    from_h1 = resample_full(frame(h1), tf)
    assert len(from_h1) >= 3
    pd.testing.assert_frame_equal(from_h1, from_m1)


async def test_deep_history_reaches_2006(provider):
    """Probe: how far back OANDA has XAU_USD (2006-03-19 when this was written)."""
    first = None
    async for c in provider.fetch_candles(
        INSTRUMENT, datetime(2000, 1, 1, tzinfo=UTC), datetime(2007, 1, 1, tzinfo=UTC), "H1"
    ):
        first = c
        break
    assert first is not None
    assert datetime.fromtimestamp(first.ts, UTC) < datetime(2006, 12, 31, tzinfo=UTC)
