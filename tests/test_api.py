import asyncio
import time

import pytest

from openchart import API_VERSION
from openchart.providers.base import Tick
from tests.conftest import NOW, Harness
from tests.ws import receive, receive_until


def push(client, tick: Tick) -> None:
    client.portal.call(client.server.feed.handle_tick, tick)


def test_health_and_version(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/version").json()["api_version"] == API_VERSION


def test_instruments(client):
    [gold] = client.get("/api/instruments").json()
    assert gold["instrument"] == "XAU_USD" and gold["decimals"] == 3
    assert gold["hours"]["open"] == {"day": "sun", "time": "18:00"}


def test_candles(client):
    body = client.get(
        "/api/candles", params={"instrument": "XAU_USD", "timeframe": "1h", "limit": 24}
    ).json()
    assert body["timeframe"] == "1h" and body["price"] == "mid" and body["decimals"] == 3
    candles = body["candles"]
    assert len(candles) == 24
    assert set(candles[0]) == {"t", "o", "h", "l", "c", "v", "complete"}
    assert [c["t"] for c in candles] == sorted(c["t"] for c in candles)


def test_candles_from_to(client):
    base = NOW // 900 * 900
    params = {"instrument": "XAU_USD", "timeframe": "15m", "from": base - 7200, "to": base - 3600}
    candles = client.get("/api/candles", params=params).json()["candles"]
    assert [c["t"] for c in candles] == list(range(base - 7200, base - 3600, 900))


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"instrument": "XAU_USD", "timeframe": "2h"}, 400),
        ({"instrument": "XAU_USD", "price": "last"}, 400),
        ({"instrument": "NOPE"}, 404),
        ({"instrument": "XAU_USD", "limit": 0}, 422),
    ],
)
def test_candles_validation(client, params, status):
    assert client.get("/api/candles", params=params).status_code == status


def test_status(client):
    status = client.get("/api/status").json()
    assert status["provider"] == "fake"
    assert "XAU_USD" in status["instruments"]


def test_websocket_handshake_and_ping(client):
    with client.websocket_connect("/ws") as ws:
        hello = receive(ws)
        assert hello["type"] == "hello" and hello["api_version"] == API_VERSION
        assert abs(hello["server_time"] - time.time()) < 5
        assert receive(ws)["type"] == "status"
        ws.send_json({"type": "ping"})
        assert receive_until(ws, "pong")["server_time"] > 0


def test_a_change_right_after_connecting_reaches_the_socket(client, monkeypatch):
    """Once the socket is open the client gets every broadcast, even when the server is
    slow to check the session (a busy CI runner). Browsers reload drawings and alerts when
    the socket opens, so a change missed in between would never show (CI failure, M6)."""
    auth = type(client.server.auth)
    check = auth.session_valid

    async def slow_check(self, token):
        await asyncio.sleep(0.3)
        return await check(self, token)

    monkeypatch.setattr(auth, "session_valid", slow_check)
    change = {"type": "drawing_changed", "op": "delete", "drawing": {"id": "d1"}}
    with client.websocket_connect("/ws") as ws:
        # Not through the API, whose own session check would give the socket time to join.
        client.portal.call(client.server.hub.broadcast, change)
        assert receive_until(ws, "drawing_changed") == change


def test_websocket_streams_ticks_and_candles(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "instrument": "XAU_USD", "timeframe": "5s"})
        assert receive_until(ws, "subscribed")["timeframe"] == "5s"

        now = time.time()
        push(client, Tick("XAU_USD", now, 2400.0, 2400.4))
        tick = receive_until(ws, "tick")
        assert (tick["instrument"], tick["bid"], tick["ask"]) == ("XAU_USD", 2400.0, 2400.4)
        candle = receive_until(ws, "candle")
        assert candle["timeframe"] == "5s" and candle["closed"] is False
        assert candle["candle"]["c"] == pytest.approx(2400.2)  # mid

        push(client, Tick("XAU_USD", now + 6, 2401.0, 2401.4))  # next 5s bar
        closed = receive_until(ws, "candle")
        assert closed["closed"] is True and closed["candle"]["v"] == 1


def test_websocket_higher_timeframe_starts_from_history(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "subscribe", "instrument": "XAU_USD", "timeframe": "1D", "price": "bid"}
        )
        receive_until(ws, "subscribed")
        # Half a minute after the last stored minute: the same trading day as the stored
        # history by construction, whatever the time of day (the clock is tests/clock.py's).
        push(client, Tick("XAU_USD", NOW + 30, 1900.0, 1900.4))
        candle = receive_until(ws, "candle")["candle"]
        assert candle["v"] > 1  # stored M1 of today's session plus the new tick
        assert candle["l"] <= 1900.0


def test_websocket_errors(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "instrument": "XAU_USD", "timeframe": "7m"})
        assert "unknown timeframe" in receive_until(ws, "error")["error"]
        ws.send_json({"type": "subscribe", "instrument": "NOPE", "timeframe": "1m"})
        assert "unknown instrument" in receive_until(ws, "error")["error"]
        ws.send_json({"type": "dance"})
        assert receive_until(ws, "error")


def test_meta_describes_timeframes_and_session(client):
    meta = client.get("/api/meta").json()
    assert [tf["name"] for tf in meta["timeframes"]][:2] == ["1s", "5s"]
    assert meta["session"] == {
        "timezone": "America/New_York",
        "day_anchor_hour": 17,
        "week_anchor_weekday": 4,
    }


def test_root_serves_web_ui_or_build_hint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "OpenChart" in response.text


def test_the_page_is_revalidated_and_built_assets_are_cached(tmp_path, monkeypatch):
    """After a frontend rebuild a reload must load the new build, not a cached page."""
    from fastapi.testclient import TestClient

    from openchart.api import app as app_module

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets" / "index-abc123.js").write_text("1")
    monkeypatch.setattr(app_module, "WEB_DIR", tmp_path)
    harness = Harness(tmp_path / "data")
    with TestClient(app_module.create_app(harness.server)) as c:
        assert c.get("/").headers["cache-control"] == "no-cache"
        cached = c.get("/assets/index-abc123.js").headers["cache-control"]
        assert "immutable" in cached
