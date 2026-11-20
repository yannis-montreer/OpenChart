"""The feed survives OANDA rejecting the credentials for a while.

See docs/decisions-feed-rejections.md.

On 15 November OANDA answered "Insufficient authorization to perform request" while the
token was valid, because OANDA itself was down, and the feed stopped for good. Now a
rejection is first taken as possibly passing: the server retries with a growing delay
(30 s doubling to 10 min, with jitter) for up to 6 hours, sends one Telegram message when
the feed goes down and one when it is back, and then catches up as after any outage.

Everything runs on the fake broker, a fake clock (the server's `now` and its retry sleep)
and a fake Telegram (an httpx mock transport that records the texts).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.config import Config
from openchart.market.instruments import METAL_HOURS
from openchart.providers.base import OHLC, AuthError, Candle, Heartbeat, ProviderError, Tick
from openchart.providers.fake import FakeProvider
from openchart.server import Server
from openchart.settings import MemorySecrets
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.bounded import call
from tests.conftest import CANDLES, EURO, GOLD, run_setup

TELEGRAM_TOKEN = "123456:telegram-SECRET-token"
REJECTED = "OANDA rejected the credentials: Insufficient authorization to perform request."
DOWN = "⚠️ Price feed down"
UP = "✅ Price feed recovered"
STOPPED = "⛔ Price feed stopped"
HOUR = 3600


def kinds(texts: list[str]) -> list[str]:
    return [
        next(k for k in (DOWN, UP, STOPPED, "⚠️ Feed stalled") if t.startswith(k)) for t in texts
    ]


class Clock:
    """The server's `now`: moved only by the test and by the retry sleep."""

    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Telegram:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    def feed(self) -> list[str]:
        """The messages about the feed (down, recovered, stopped, stalled)."""
        return [t for t in self.texts if t.startswith((DOWN, UP, STOPPED, "⚠️ Feed stalled"))]


class FlakyBroker(FakeProvider):
    """A fake broker that fails every request while `failure` is set (a factory for the
    exception), then serves its candles and a stream that connects and stays quiet."""

    def __init__(self, candles, failure=None):
        super().__init__({"XAU_USD": candles}, instruments=[GOLD, EURO])
        self.failure = failure
        self.requests = 0  # every request, refused or not
        self.refused = 0

    def _check(self):
        self.requests += 1
        if self.failure is not None:
            self.refused += 1
            raise self.failure()

    async def fetch_candles(self, instrument, start, end, granularity="M1"):
        self._check()
        async for c in super().fetch_candles(instrument, start, end, granularity):
            yield c

    async def fetch_page_before(self, instrument, end, granularity="M1"):
        self._check()
        return await super().fetch_page_before(instrument, end, granularity)

    async def stream_ticks(self, instruments):
        self._check()
        yield Heartbeat(time.time())
        await asyncio.Event().wait()  # connected, quiet: the tests push ticks themselves


def rejected() -> AuthError:
    return AuthError(REJECTED)


class Sleeper:
    """The retry sleep: records each delay and moves the fake clock by it. `hold_at`: the
    sleep with that number waits (in real time) until the test lets it go; `heal_after`:
    the broker recovers after that many sleeps."""

    def __init__(self, clock: Clock, broker: FlakyBroker, *, heal_after=None, hold_at=None):
        self.clock, self.broker = clock, broker
        self.heal_after, self.hold_at = heal_after, hold_at
        self.delays: list[float] = []
        self.held = False
        self.release = False

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) == self.hold_at:
            self.held = True
            # Released from the test's thread: an asyncio.Event is not thread-safe.
            while not self.release:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        self.clock.t += delay
        if self.heal_after is not None and len(self.delays) >= self.heal_after:
            self.broker.failure = None
        await asyncio.sleep(0)


def seed(config, candles):
    async def run():
        async with await Database.open(config.db_path) as db:
            store = CandleStore(db.conn)
            # Disabled: the feed starts once, when the setup enables it.
            await store.upsert_instrument("fake", GOLD, METAL_HOURS, enabled=False)
            await store.upsert_m1("fake", "XAU_USD", candles)
            # Telegram is set up before the feed starts (it starts with the server).
            await db.conn.execute(
                "INSERT INTO settings (key, value) VALUES ('telegram_chat_id', ?)",
                (json.dumps("42"),),
            )
            await db.conn.commit()

    asyncio.run(run())


def wait_for(predicate, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def settle(c, rounds: int = 3) -> None:
    for _ in range(rounds):
        call(c, c.server.alerts.dispatcher.drain)
        time.sleep(0.05)


class Rig:
    """A set-up server on the flaky broker, with Telegram, the fake clock and the sleeper."""

    def __init__(self, tmp_path, broker, *, stored=CANDLES, **sleeper):
        self.config = replace(Config(), data_dir=tmp_path)
        seed(self.config, stored)
        self.secrets = MemorySecrets()
        self.secrets.set("telegram_token", TELEGRAM_TOKEN)
        self.broker = broker
        self.clock = Clock(time.time())
        self.telegram = Telegram()
        self.sleeper = Sleeper(self.clock, broker, **sleeper)
        self.server = Server(
            self.config,
            provider=broker,
            secrets=self.secrets,
            provider_factory=self._factory,  # setup's check too: never the real OANDA
            indicator_worker=False,
        )
        self.server.now = self.clock
        self.server.feed_sleep = self.sleeper
        self.server.telegram_transport = self.telegram.transport

    @staticmethod
    def _factory(_oanda):
        return FakeProvider({"XAU_USD": CANDLES}, instruments=[GOLD, EURO])

    def __enter__(self) -> TestClient:
        self._client = TestClient(create_app(self.server))
        c = self._client.__enter__()
        c.server, c.rig = self.server, self
        assert run_setup(c).status_code == 200
        return c

    def __exit__(self, *exc):
        return self._client.__exit__(*exc)


def status(c) -> dict:
    return c.get("/api/status").json()


# The retry delay -----------------------------------------------------------------------


def test_the_delay_doubles_from_30_s_to_10_min_with_jitter_below_it():
    from openchart.market.rejections import retry_delay

    top = [retry_delay(n, rand=lambda: 0.0) for n in range(8)]
    assert top == [30, 60, 120, 240, 480, 600, 600, 600]
    low = [retry_delay(n, rand=lambda: 1.0) for n in range(8)]
    assert low == [0.8 * d for d in top]  # jitter takes off up to a fifth, never adds
    assert retry_delay(10_000, rand=lambda: 0.0) == 600  # no overflow on a long outage
    spread = {retry_delay(2) for _ in range(50)}
    assert len(spread) > 1 and all(96 <= d <= 120 for d in spread)


def test_the_bounds():
    from openchart.market import rejections

    assert rejections.RETRY_FIRST == 30
    assert rejections.RETRY_MAX == 600
    assert rejections.RETRY_LIMIT == 6 * HOUR


# The server ----------------------------------------------------------------------------


def test_a_short_outage_recovers_with_one_message_down_and_one_up(tmp_path):
    broker = FlakyBroker(CANDLES, failure=rejected)
    rig = Rig(tmp_path, broker, heal_after=4, hold_at=2)
    with rig as c:
        wait_for(lambda: rig.sleeper.held, "the second retry")
        settle(c)
        # Down: one message, the state in the status, the engine still running.
        [down] = rig.telegram.feed()
        assert down.startswith(DOWN) and "Insufficient authorization" in down
        assert "keeps retrying" in down
        assert "request. The token may still be valid" in down  # one full stop, not two
        s = status(c)
        assert s["provider_state"] == "retrying"
        assert s["error"] == REJECTED
        retry = s["retry"]
        assert retry["reason"] == "credentials_rejected"
        assert retry["attempts"] == 2
        assert retry["since"] == pytest.approx(rig.clock.t - rig.sleeper.delays[0])
        assert retry["next_attempt"] == pytest.approx(rig.clock.t + rig.sleeper.delays[1])
        assert retry["next_attempt_in"] == pytest.approx(rig.sleeper.delays[1], abs=1)
        assert retry["gives_up_at"] == pytest.approx(retry["since"] + 6 * HOUR)
        assert not c.server.alerts._tasks[1].done()  # the engine's loop runs on

        rig.sleeper.release = True
        wait_for(lambda: c.server.status()["provider_state"] == "connected", "the recovery")
        settle(c)
        assert len(rig.sleeper.delays) == 4
        assert kinds(rig.telegram.feed()) == [DOWN, UP]  # none per retry
        outage = int(sum(rig.sleeper.delays) // 60)  # 30 + 60 + 120 + 240 s, jittered
        assert f"again after {outage} min." in rig.telegram.feed()[1]
        s = status(c)
        assert s["retry"] is None and s["error"] is None


def test_the_delays_grow_and_the_broker_is_not_hammered(tmp_path):
    broker = FlakyBroker(CANDLES, failure=rejected)
    rig = Rig(tmp_path, broker, heal_after=7)
    with rig as c:
        wait_for(lambda: c.server.status()["provider_state"] == "connected", "the recovery")
        d = rig.sleeper.delays
        assert len(d) == 7
        assert all(
            0.8 * b <= x <= b for x, b in zip(d, [30, 60, 120, 240, 480, 600, 600], strict=True)
        )
        # Each attempt stops at the first refused request.
        assert broker.refused <= 2 * len(d) + 2


def test_a_rejection_that_never_ends_stops_after_6_hours_with_the_same_error(tmp_path):
    broker = FlakyBroker(CANDLES, failure=rejected)
    rig = Rig(tmp_path, broker)
    with rig as c:
        wait_for(lambda: c.server._feed_task is not None and c.server._feed_task.done(), "stop")
        settle(c)
        waited = sum(rig.sleeper.delays)
        assert 6 * HOUR - 600 <= waited < 6 * HOUR + 600
        assert len(rig.sleeper.delays) < 45  # about one attempt every 10 min
        s = status(c)
        assert s["error"] == REJECTED  # what it said before this change
        assert s["retry"] is None and s["provider_state"] != "retrying"
        down, stopped = rig.telegram.feed()
        assert down.startswith(DOWN)
        assert stopped.startswith(STOPPED) and "6 h" in stopped and "token" in stopped
        refused = broker.refused
        time.sleep(0.2)
        assert broker.refused == refused  # no attempt after the stop


def test_a_token_that_cannot_be_one_stops_at_once_as_before(tmp_path):
    """A rejection that is certain (here: a token with a space, refused before any request)
    is not retried, and sends nothing, as before."""
    broker = FlakyBroker(CANDLES, failure=lambda: AuthError("bad token", certain=True))
    rig = Rig(tmp_path, broker)
    with rig as c:
        wait_for(lambda: c.server._feed_task is not None and c.server._feed_task.done(), "stop")
        settle(c)
        assert rig.sleeper.delays == []
        assert status(c)["error"] == "bad token"
        assert rig.telegram.feed() == []


def test_oanda_token_problems_are_certain_and_its_refusals_are_not():
    from openchart.config import OandaConfig
    from openchart.providers.oanda import OandaProvider

    async def go(token, transport):
        cfg = OandaConfig(token=token, account_id="101-001-1-001", environment="practice")
        p = OandaProvider(cfg, transport=transport, request_delay=0)
        try:
            await p.list_instruments()
        except AuthError as exc:
            return exc
        finally:
            await p.aclose()

    refuse = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"errorMessage": "Insufficient authorization"})
    )
    assert asyncio.run(go("a b", refuse)).certain is True
    assert asyncio.run(go("abc", refuse)).certain is False


def test_a_new_rejection_after_recovering_is_a_new_outage(tmp_path):
    broker = FlakyBroker(CANDLES, failure=rejected)
    rig = Rig(tmp_path, broker, heal_after=1)
    with rig as c:
        wait_for(lambda: c.server.status()["provider_state"] == "connected", "the recovery")
        settle(c)
        assert len(rig.telegram.feed()) == 2
        rig.sleeper.heal_after = 3
        broker.failure = rejected
        call(c, c.server.reconfigure)  # the stream reconnects and is refused
        wait_for(lambda: c.server.status()["provider_state"] == "connected", "the 2nd recovery")
        settle(c)
        assert kinds(rig.telegram.feed()) == [DOWN, UP] * 2


def test_the_watchdog_does_not_add_stall_messages_during_the_outage(tmp_path):
    broker = FlakyBroker(CANDLES, failure=rejected)
    rig = Rig(tmp_path, broker, hold_at=1)
    with rig as c:
        wait_for(lambda: rig.sleeper.held, "the first retry")
        rig.clock.t += 3600  # an hour into the outage, market open
        c.server.alerts.watch()
        settle(c)
        assert kinds(rig.telegram.feed()) == [DOWN]
        rig.sleeper.release = True


# Alerts during the outage ---------------------------------------------------------------


def flat(t: int, price: float) -> Candle:
    m = OHLC(price, price + 0.5, price - 0.5, price)
    return Candle(t, m, m, m, 1)


def crossing(t: int, o: float, h: float, low: float, c: float) -> Candle:
    m = OHLC(o, h, low, c)
    return Candle(t, m, m, m, 1)


def outage_scenario(tmp_path, failure):
    """Prices are stored up to `t1`; the last tick is at `t1`; the broker fails from then.
    An alert on a level, one on a horizontal line and one on an indicator are made during
    the outage. Meanwhile the price crossed 1900 up and down; the broker recovers, the
    first tick after it reveals the gap. Returns (alert events, feed messages)."""
    now = int(time.time()) // 60 * 60
    t1 = now - 2 * HOUR
    before = [flat(c.ts, 1899.0) for c in CANDLES if t1 - 6 * HOUR <= c.ts < t1]
    during = [flat(t, 1899.0) for t in range(t1, now, 60)]
    during[40] = crossing(t1 + 2400, 1899.0, 1901.0, 1898.9, 1900.5)
    during[41] = crossing(t1 + 2460, 1900.5, 1900.6, 1898.0, 1898.5)
    broker = FlakyBroker(before + during, failure=failure)
    rig = Rig(tmp_path, broker, stored=before, hold_at=2)
    with rig as c:
        server = c.server
        wait_for(lambda: rig.sleeper.held or broker.refused >= 3, "the outage")
        call(c, server.feed.handle_tick, Tick("XAU_USD", t1 + 5, 1898.9, 1899.1))
        # Alerts made during the outage.
        level = {
            "instrument": "XAU_USD",
            "type": "level_cross",
            "level": 1900,
            "frequency": "repeating",
        }
        assert c.post("/api/alerts", json=level).status_code == 200
        hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 1900.0}]}
        drawing = c.post("/api/drawings", json=hline).json()
        line = {
            "instrument": "XAU_USD",
            "type": "line_cross",
            "drawing_id": drawing["id"],
            "frequency": "repeating",
        }
        assert c.post("/api/alerts", json=line).status_code == 200
        chart = f"/api/charts/{server.source}/XAU_USD/indicators"
        sma = {"uid": "s1", "kind": "builtin", "id": "sma", "params": {"length": 1}}
        assert c.put(chart, json={"indicators": [sma]}).status_code == 200
        indicator = {
            "instrument": "XAU_USD",
            "type": "indicator",
            "timeframe": "1m",
            "frequency": "repeating",
            "indicator": {"uid": "s1", "output": "sma", "condition": "cross_value", "value": 1900},
        }
        assert c.post("/api/alerts", json=indicator).status_code == 200
        ind = server.alerts.indicators
        call(c, ind.stop)  # the test runs the indicator evaluation itself
        ind.evaluator.fresh = lambda _i, _t: True
        call(c, ind.run_once, float(t1 + 30))  # armed on the last stored bar

        broker.failure = None  # OANDA is back
        rig.sleeper.release = True
        wait_for(lambda: server.status()["provider_state"] == "connected", "the recovery")
        call(c, server.feed.handle_tick, Tick("XAU_USD", time.time(), 1898.3, 1898.5))
        wait_for(lambda: not server.alerts.engine.gaps, "the catch-up")
        call(c, ind.run_once, time.time())
        settle(c)
        events = c.get("/api/alerts/events?limit=100").json()
        kinds = {a["id"]: a["type"] for a in c.get("/api/alerts").json()}
        got = sorted(
            (kinds[e["alert_id"]], e["ts"], e["cross"], e["late"], round(e["price"], 3))
            for e in events
        )
        return got, rig.telegram.feed()


@pytest.mark.parametrize(
    "failure",
    [rejected, lambda: ProviderError("OANDA request failed after retries: HTTP 503")],
    ids=["credentials-rejected", "network-outage"],
)
def test_alerts_armed_during_the_outage_fire_late_after_recovery(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("openchart.server.FEED_RETRY_SECONDS", 0.05)
    events, _ = outage_scenario(tmp_path, failure)
    by_type = {}
    for kind, _ts, cross, late, _price in events:
        by_type.setdefault(kind, []).append((cross, late))
    both = [("up", "reconnect"), ("down", "reconnect")]  # in time order
    assert by_type == {"level_cross": both, "line_cross": both, "indicator": both}


def test_line_and_indicator_alerts_behave_exactly_as_after_a_network_outage(tmp_path, monkeypatch):
    """The same outage, once as rejected credentials and once as a network failure (the
    path this change does not touch): the same events, field for field."""
    monkeypatch.setattr("openchart.server.FEED_RETRY_SECONDS", 0.05)
    auth, auth_messages = outage_scenario(tmp_path / "auth", rejected)
    net, net_messages = outage_scenario(
        tmp_path / "net", lambda: ProviderError("OANDA request failed after retries: HTTP 503")
    )
    assert auth == net and len(auth) == 6
    assert kinds(auth_messages) == [DOWN, UP]
    assert net_messages == []  # a network failure sends what it sent before: nothing here
