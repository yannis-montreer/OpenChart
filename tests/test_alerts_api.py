"""Alerts through the server: API, firing on live ticks, Telegram delivery, catch-up after
a freeze, watchdog (SPEC sections 7 and 9)."""

import json
import time

import httpx
import pytest

from openchart.alerts.notify import Dispatcher, NotifierUnavailable
from openchart.alerts.watchdog import FeedView, Watchdog
from openchart.providers.base import OHLC, Candle, Tick
from openchart.store.alerts import AlertEvent
from tests.ws import receive_until

TELEGRAM_TOKEN = "123456:telegram-SECRET-token"


def push(client, t: float, price: float) -> None:
    client.portal.call(client.server.feed.handle_tick, Tick("XAU_USD", t, price - 0.1, price + 0.1))


def settle(client, rounds: int = 3) -> None:
    """Let the server's background tasks (fires, delivery) run."""
    for _ in range(rounds):
        client.portal.call(client.server.alerts.dispatcher.drain)
        time.sleep(0.05)


def with_telegram(client, status: int = 200, body: dict | None = None) -> list:
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(status, json=body or {"ok": True})

    client.server.telegram_transport = httpx.MockTransport(handler)
    out = client.put(
        "/api/settings", json={"telegram_token": TELEGRAM_TOKEN, "telegram_chat_id": "42"}
    )
    assert out.status_code == 200
    return seen


LEVEL = {"instrument": "XAU_USD", "type": "level_cross", "level": 2000.0, "frequency": "repeating"}


def test_create_list_update_delete(client):
    with client.websocket_connect("/ws") as ws:
        created = client.post("/api/alerts", json=LEVEL).json()
        assert receive_until(ws, "alert_changed")["alert"]["id"] == created["id"]
    assert created["status"] == "active" and created["notify"] == ["telegram", "app"]
    assert created["name"] == "XAU_USD, 1m Crossing 2000"
    assert [a["id"] for a in client.get("/api/alerts").json()] == [created["id"]]

    stopped = client.put(f"/api/alerts/{created['id']}", json={"status": "stopped"}).json()
    assert stopped["status"] == "stopped"
    renamed = client.put(f"/api/alerts/{created['id']}", json={"name": "Gold 2000"}).json()
    assert renamed["name"] == "Gold 2000"

    assert client.delete(f"/api/alerts/{created['id']}").status_code == 200
    assert client.get("/api/alerts").json() == []
    assert client.delete(f"/api/alerts/{created['id']}").status_code == 404


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"type": "line_cross", "drawing_id": "nope"}, 400),
        ({"level": None}, 400),
        ({"timeframe": "7m"}, 422),
        ({"timeframe": "5s"}, 422),
        ({"direction": "sideways"}, 422),
        ({"notify": ["email"]}, 422),
        ({"expires_at": 1000}, 400),
        ({"instrument": "NOPE"}, 404),
    ],
)
def test_validation(client, change, status):
    assert client.post("/api/alerts", json={**LEVEL, **change}).status_code == status


def test_a_level_alert_fires_on_live_ticks_and_reaches_telegram(client):
    seen = with_telegram(client)
    alert = client.post("/api/alerts", json={**LEVEL, "trigger_price": "bid"}).json()
    now = time.time()
    with client.websocket_connect("/ws") as ws:
        push(client, now, 1999.0)
        push(client, now + 1, 2001.0)  # bid 2000.9: crossed up
        message = receive_until(ws, "alert_triggered")
    event = message["event"]
    assert message["alert"]["id"] == alert["id"] and message["alert"]["fire_count"] == 1
    assert (
        event["cross"] == "up" and event["price"] == pytest.approx(2000.9) and event["late"] is None
    )
    settle(client)
    [text] = [t for t in seen if t.startswith("🔔")]
    assert "XAU_USD crossed up: 2,000.900 (bid), line 2,000.000" in text
    [logged] = client.get("/api/alerts/events").json()
    assert logged["delivered"] is True and logged["attempts"] == 1


def test_an_alert_on_a_line_and_deleting_the_line(client):
    hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    drawing = client.post("/api/drawings", json=hline).json()
    body = {
        "instrument": "XAU_USD",
        "type": "line_cross",
        "drawing_id": drawing["id"],
        "direction": "down",
    }
    alert = client.post("/api/alerts", json=body).json()
    assert alert["name"] == "XAU_USD, 1m Crossing Down horizontal line"

    # Dragging the line across the price re-arms the alert instead of firing it.
    now = time.time()
    push(client, now, 2001.0)
    client.put(f"/api/drawings/{drawing['id']}", json={"points": [{"t": 1, "p": 2002.0}]})
    push(client, now + 1, 2001.0)
    settle(client)
    assert client.get("/api/alerts/events").json() == []
    push(client, now + 2, 2003.0)
    push(client, now + 3, 2001.5)  # a real down-cross
    settle(client)
    assert len(client.get("/api/alerts/events").json()) == 1

    with client.websocket_connect("/ws") as ws:
        client.delete(f"/api/drawings/{drawing['id']}")
        message = receive_until(ws, "alert_changed")
        assert message["op"] == "delete" and message["alert"]["id"] == alert["id"]
    assert client.get("/api/alerts").json() == []


def test_deleting_an_alert_leaves_its_line(client):
    hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    drawing = client.post("/api/drawings", json=hline).json()
    body = {"instrument": "XAU_USD", "type": "line_cross", "drawing_id": drawing["id"]}
    first = client.post("/api/alerts", json=body).json()
    second = client.post("/api/alerts", json={**body, "direction": "up"}).json()

    with client.websocket_connect("/ws") as ws:
        assert client.delete(f"/api/alerts/{first['id']}").status_code == 200
        message = receive_until(ws, "alert_changed")
        assert message["op"] == "delete" and message["alert"]["id"] == first["id"]
    # The line and the other alert on it are untouched.
    [kept] = client.get("/api/drawings", params={"instrument": "XAU_USD"}).json()
    assert kept == drawing
    by_line = client.get("/api/alerts", params={"drawing_id": drawing["id"]}).json()
    assert [a["id"] for a in by_line] == [second["id"]]
    assert client.server.alerts.engine.alerts.keys() == {second["id"]}


def test_the_alert_state_of_a_line_is_exposed(client):
    """The line's bell (toolbar and on the chart) shows the state of its alerts."""
    seen = with_telegram(client)
    hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    line = client.post("/api/drawings", json=hline).json()
    other = client.post("/api/drawings", json=hline).json()
    now = int(time.time())
    body = {"instrument": "XAU_USD", "type": "line_cross", "drawing_id": line["id"]}
    alert = client.post("/api/alerts", json={**body, "expires_at": now + 60}).json()

    def states(drawing_id):
        found = client.get("/api/alerts", params={"drawing_id": drawing_id}).json()
        return [(a["id"], a["drawing_id"], a["status"]) for a in found]

    assert states(line["id"]) == [(alert["id"], line["id"], "active")]
    assert states(other["id"]) == []
    client.put(f"/api/alerts/{alert['id']}", json={"status": "stopped"})
    assert states(line["id"]) == [(alert["id"], line["id"], "stopped")]
    client.put(f"/api/alerts/{alert['id']}", json={"status": "active"})

    # It expires: the state says so, the browsers hear it and Telegram is told.
    with client.websocket_connect("/ws") as ws:
        client.portal.call(client.server.alerts.step, now + 61)
        message = receive_until(ws, "alert_changed")
    assert message["alert"]["status"] == "expired"
    assert states(line["id"]) == [(alert["id"], line["id"], "expired")]
    settle(client)
    [expired] = [t for t in seen if t.startswith("⌛")]
    assert alert["name"] in expired and "no longer watches" in expired


def test_without_telegram_the_event_records_why(client):
    client.post("/api/alerts", json=LEVEL)
    now = time.time()
    push(client, now, 1999.0)
    push(client, now + 1, 2001.0)
    settle(client)
    [event] = client.get("/api/alerts/events").json()
    assert event["delivered"] is False and "not set up" in event["error"]


def test_app_only_alerts_need_no_delivery(client):
    client.post("/api/alerts", json={**LEVEL, "notify": ["app"]})
    now = time.time()
    push(client, now, 1999.0)
    push(client, now + 1, 2001.0)
    settle(client)
    [event] = client.get("/api/alerts/events").json()
    assert event["delivered"] is True and event["attempts"] == 0


async def test_failed_deliveries_are_retried(tmp_path):
    from openchart.store.alerts import AlertStore
    from openchart.store.sqlite import Database

    attempts = []

    class Flaky:
        name = "telegram"

        async def send(self, text):
            attempts.append(text)
            if len(attempts) < 3:
                raise OSError("network down")

    async def notifier():
        return Flaky()

    async def no_wait(_):
        pass

    async with await Database.open(tmp_path / "t.db") as db:
        store = AlertStore(db.conn)
        event = await store.add_event(AlertEvent(0, "a1", 1, 2000.0, 2000.0, "up", "hi"))
        dispatcher = Dispatcher(store, notifier, sleep=no_wait)
        dispatcher.event(event, "hi")
        await dispatcher.drain()
        saved = await store.event(event.id)
        assert saved.delivered and saved.attempts == 3 and saved.error is None

        async def nobody():
            raise NotifierUnavailable("Telegram is not set up")

        lost = await store.add_event(AlertEvent(0, "a1", 2, 2000.0, 2000.0, "up", "x"))
        dispatcher = Dispatcher(store, notifier, sleep=no_wait)
        dispatcher.notifier = lambda: nobody()
        dispatcher.event(lost, "x")
        await dispatcher.drain()
        saved = await store.event(lost.id)
        assert not saved.delivered and saved.attempts == 1  # no point retrying


# Sleep and wake-up


def candle(ts: int, o: float, h: float, low: float, c: float) -> Candle:
    m = OHLC(o, h, low, c)
    return Candle(ts, m, m, m, 1)


def test_a_cross_during_a_freeze_fires_late_with_a_freeze_message(client):
    seen = with_telegram(client)
    # Far from the test server's synthetic prices (2000 +- 10), which this test replaces.
    client.post("/api/alerts", json={**LEVEL, "level": 1900.0})
    server = client.server
    wake = time.time()
    t0 = int(wake) // 60 * 60 - 3 * 3600
    push(client, t0 + 5, 1899.0)  # last observation before the computer slept

    # The repair stored the candles of the frozen period: the price crossed and came back.
    frozen = [candle(t, 1899.0, 1899.5, 1898.5, 1899.0) for t in range(t0 + 60, int(wake), 60)]
    frozen[59] = candle(t0 + 3600, 1899.0, 1901.0, 1898.9, 1900.5)
    frozen[60] = candle(t0 + 3660, 1900.5, 1900.6, 1898.0, 1898.5)
    client.portal.call(server.store.upsert_m1, "fake", "XAU_USD", frozen)

    client.portal.call(server.alerts.on_wake, t0 + 6, wake)  # the feed noticed the freeze
    push(client, wake, 1898.4)  # the first tick after waking reveals the gap
    for _ in range(40):
        time.sleep(0.1)
        if not server.alerts.engine.gaps and server.alerts.freeze is None:
            break
    settle(client)
    events = client.get("/api/alerts/events").json()
    assert [(e["cross"], e["late"]) for e in reversed(events)] == [("up", "wake"), ("down", "wake")]
    assert events[-1]["ts"] == t0 + 3600 + 40
    alerts_text = [t for t in seen if t.startswith("🔔")]
    assert all("detected after wake-up" in t for t in alerts_text) and len(alerts_text) == 2
    [freeze] = [t for t in seen if t.startswith("⏸️")]
    assert "2 alerts fired late" in freeze and "may have been late" in freeze


def test_the_engine_announces_its_start(harness):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from tests.conftest import run_setup

    seen: list = []

    def handler(request):
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    harness.server.telegram_transport = httpx.MockTransport(handler)
    harness.secrets.set("telegram_token", TELEGRAM_TOKEN)
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        run_setup(c, telegram_chat_id="42")
    from openchart.server import Server

    again = Server(harness.config, provider=harness.provider, secrets=harness.secrets)
    again.telegram_transport = httpx.MockTransport(handler)
    with TestClient(create_app(again)) as c:
        c.portal.call(again.alerts.dispatcher.drain)
    assert any(t.startswith("▶️ OpenChart alert engine started") for t in seen)


def test_alert_state_survives_a_restart(harness):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from openchart.server import Server
    from tests.conftest import PASSWORD, run_setup

    harness.seed_candles()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        run_setup(c)
        created = c.post("/api/alerts", json=LEVEL).json()
        push(c, time.time(), 1999.0)
    again = Server(harness.config, provider=harness.provider, secrets=harness.secrets)
    with TestClient(create_app(again)) as c:
        c.post("/api/login", json={"password": PASSWORD})
        engine = again.alerts.engine
        assert engine.alerts[created["id"]].state.s_prev == pytest.approx(-1.0)
        assert engine.last_eval["XAU_USD"] > 0


# Watchdog


def test_watchdog_stall_and_recovery():
    now = [1000.0]
    dog = Watchdog(stall_seconds=300, now=lambda: now[0])
    assert dog.check({"XAU_USD": FeedView(990.0, True)}) == []
    now[0] = 1300.0
    [stalled] = dog.check({"XAU_USD": FeedView(990.0, True)}, "reconnecting")
    assert (
        stalled.startswith("⚠️ Feed stalled: no XAU_USD price for 5 min")
        and "reconnecting" in stalled
    )
    assert dog.check({"XAU_USD": FeedView(990.0, True)}) == []  # once
    now[0] = 1900.0
    [back] = dog.check({"XAU_USD": FeedView(1890.0, True)})
    assert back.startswith("✅ Feed recovered: XAU_USD prices are back after 15 min")


def test_watchdog_is_quiet_while_the_market_is_closed_and_after_a_wake_up():
    now = [1000.0]
    dog = Watchdog(stall_seconds=300, now=lambda: now[0])
    now[0] = 5000.0
    assert dog.check({"XAU_USD": FeedView(900.0, False)}) == []  # weekend
    dog.reset()  # woke up
    now[0] = 5100.0
    assert dog.check({"XAU_USD": FeedView(900.0, True)}) == []  # a grace period first
    now[0] = 5400.0
    assert len(dog.check({"XAU_USD": FeedView(900.0, True)})) == 1
