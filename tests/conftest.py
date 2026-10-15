"""Shared fixtures: a server on a temporary database with a fake broker."""

import asyncio
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.config import Config
from openchart.market.instruments import METAL_HOURS
from openchart.market.sessions import TradingHours
from openchart.providers.base import InstrumentInfo
from openchart.providers.fake import FakeProvider, synthetic_m1
from openchart.server import Server
from openchart.settings import MemorySecrets
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

GOLD = InstrumentInfo("XAU_USD", "XAU_USD", "Gold", 3, 0.01, "METAL")
EURO = InstrumentInfo("EUR_USD", "EUR_USD", "EUR/USD", 5, 0.0001, "CURRENCY")
# A made-up third instrument unlike both: 1 decimal, a provider symbol of its own, a
# category with no default hours, and hours on Berlin time with a daily break over
# midnight, reported by the provider itself.
ODD_HOURS = TradingHours(
    "Europe/Berlin", "mon", "01:15", "fri", "22:00", daily_breaks=(("22:00", "01:15"),)
)
ODD = InstrumentInfo("FAKE_IDX", "FAKEIDX.cash", "Fake Index", 1, 1.0, "CFD", hours=ODD_HOURS)
NOW = int(time.time()) // 60 * 60
CANDLES = synthetic_m1(NOW - 3 * 86400, NOW)
PASSWORD = "correct horse battery"
TOKEN = "oanda-token-SECRET-abcd1234"


class Harness:
    def __init__(self, tmp_path, *, indicator_launchers=None):
        self.config = replace(Config(), data_dir=tmp_path)
        self.secrets = MemorySecrets()
        self.provider = FakeProvider({"XAU_USD": CANDLES}, instruments=[GOLD, EURO])
        self.factory_calls = []

        def factory(oanda):
            self.factory_calls.append(oanda)
            if oanda.token != TOKEN:
                return FakeProviderRejecting()
            return FakeProvider({"XAU_USD": CANDLES}, instruments=[GOLD, EURO])

        # The fake stream stays silent; tests push ticks through the feed themselves. No
        # indicator worker unless a test gives launchers (tests/worker_helpers.py).
        self.server = Server(
            self.config,
            provider=self.provider,
            secrets=self.secrets,
            provider_factory=factory,
            indicator_worker=indicator_launchers is not None,
            indicator_launchers=indicator_launchers,
        )

    def seed_candles(self):
        async def seed():
            async with await Database.open(self.config.db_path) as db:
                store = CandleStore(db.conn)
                await store.upsert_instrument("fake", GOLD, METAL_HOURS)
                await store.upsert_m1("fake", "XAU_USD", CANDLES)

        asyncio.run(seed())


class FakeProviderRejecting(FakeProvider):
    async def list_instruments(self):
        from openchart.providers.base import AuthError

        raise AuthError("OANDA rejected the credentials: Insufficient authorization")

    async def test_connection(self):
        from openchart.providers.base import TestResult

        return TestResult(False, "OANDA rejected the credentials: Insufficient authorization")


SETUP = {
    "password": PASSWORD,
    "oanda_environment": "practice",
    "oanda_account_id": "101-001-1-001",
    "oanda_token": TOKEN,
    "instrument": "XAU_USD",
}


def run_setup(client: TestClient, **overrides):
    body = {**SETUP, "setup_code": client.server.auth.setup_code, **overrides}
    return client.post("/api/setup", json=body)


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def fresh(harness):
    """A server that has not been set up."""
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.harness = harness
        yield c


@pytest.fixture
def client(harness):
    """A server that is set up, with candles stored, and a logged-in client."""
    harness.seed_candles()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.harness = harness
        assert run_setup(c).status_code == 200
        yield c
