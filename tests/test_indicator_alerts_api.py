"""Indicator alerts through the alert routes (docs/designs/indicator-alerts.md section 6):
create, list, edit, stop and delete, every field checked on the server, the stored state,
the same login and same-origin rules as the other alert routes, and the "Delete data..."
flow counting and removing them."""

from __future__ import annotations

import pytest

from openchart.alerts.indicator_rules import (
    ConditionError,
    check_condition,
    warnings_from_checks,
)
from openchart.indicators._custom import indicator_from_json
from tests.bounded import call
from tests.test_instrument_delete import E, delete, preview, removed  # noqa: F401
from tests.test_instruments import market  # noqa: F401  (the fixture `removed` needs)
from tests.ws import receive_until

RSI = {"uid": "rsi1", "kind": "builtin", "id": "rsi", "params": {"length": 14}}
MACD = {"uid": "macd1", "kind": "builtin", "id": "macd", "params": {}}


def put_chart(client, instances, instrument="XAU_USD"):
    url = f"/api/charts/{client.server.source}/{instrument}/indicators"
    r = client.put(url, json={"indicators": instances})
    assert r.status_code == 200, r.text
    return r.json()


def body(**indicator):
    cond = {"uid": "rsi1", "output": "rsi", "condition": "cross_value", "value": 30.0}
    cond.update(indicator)
    return {"instrument": "XAU_USD", "type": "indicator", "timeframe": "1h", "indicator": cond}


@pytest.fixture
def ia(client):
    put_chart(client, [RSI, MACD])
    return client


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


# Create, list, edit, stop, delete ----------------------------------------------------


def test_create_takes_a_snapshot_of_the_instance(ia):
    with ia.websocket_connect("/ws") as ws:
        alert = ok(ia.post("/api/alerts", json=body() | {"direction": "up"}))
        assert receive_until(ws, "alert_changed")["alert"]["id"] == alert["id"]
    assert alert["type"] == "indicator" and alert["trigger_mode"] == "candle_close"
    assert alert["timeframe"] == "1h" and alert["direction"] == "up"
    cond = alert["indicator"]
    assert cond["uid"] == "rsi1" and cond["kind"] == "builtin" and cond["id"] == "rsi"
    assert cond["digest"] == "builtin" and cond["name"] == "RSI"
    # The value inputs, filled in; the colours and widths are not part of it.
    assert cond["params"]["length"] == 14 and "color" not in cond["params"]
    assert "width" not in cond["params"] and cond["params"]["overbought"] == 70
    assert alert["stop_reason"] is None
    assert alert["state"] == {
        "last_bar": None,
        "armed": False,
        "error": None,
        "error_since": None,
        "warnings": [],
        "behind": False,
    }
    assert alert["name"] == "XAU_USD, 1h RSI rsi crossing up 30"
    assert ok(ia.get("/api/alerts")) == [alert]
    assert ok(ia.get("/api/alerts?uid=rsi1")) == [alert]
    assert ok(ia.get("/api/alerts?uid=macd1")) == []


def test_crossing_another_output_and_a_level(ia):
    macd = ok(
        ia.post(
            "/api/alerts",
            json=body(
                uid="macd1",
                output="macd",
                condition="cross_output",
                other_output="signal",
                value=None,
            ),
        )
    )
    assert macd["indicator"]["other_output"] == "signal" and macd["indicator"]["value"] is None
    level = ok(
        ia.post(
            "/api/alerts",
            json=body(condition="cross_output", other_output="level:1", value=None),
        )
    )
    assert level["indicator"]["other_output"] == "level:1"


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"indicator": None}, 400),  # no indicator
        ({"drawing_id": "x"}, 400),
        ({"level": 1.0}, 400),
        ({"trigger_mode": "touch"}, 400),  # closed bars only
        ({"timeframe": None}, 400),
        ({"timeframe": "5s"}, 422),  # D3: no seconds timeframes
        ({"timeframe": "30s"}, 422),
        ({"timeframe": "7m"}, 422),
        ({"instrument": "NOPE"}, 404),
        ({"expires_at": 1000}, 400),
        ({"frequency": "sometimes"}, 422),
        ({"cooldown_s": -1}, 422),
        ({"notify": ["email"]}, 422),
        ({"trigger_price": "last"}, 422),
    ],
)
def test_alert_fields_are_checked(ia, change, status):
    payload = {**body(), **change}
    payload = {k: v for k, v in payload.items() if v is not None}
    assert ia.post("/api/alerts", json=payload).status_code == status
    assert ok(ia.get("/api/alerts")) == []


@pytest.mark.parametrize(
    ("indicator", "status"),
    [
        ({"uid": "nope"}, 400),  # not on the chart
        ({"uid": "bad uid!"}, 422),
        ({"output": "nope"}, 400),
        ({"output": "x" * 49}, 422),
        ({"output": "level:1"}, 400),  # a level is not a crossing output
        ({"condition": "touch"}, 422),
        ({"value": None}, 400),  # cross_value needs a value
        ({"value": "30"}, 200),  # pydantic's lax float: a numeric string is a number
        ({"other_output": "signal"}, 400),  # cross_value takes none
        ({"condition": "cross_output", "value": None}, 400),  # no other_output
        ({"condition": "cross_output", "other_output": "rsi", "value": None}, 400),  # itself
        ({"condition": "cross_output", "other_output": "nope", "value": None}, 400),
        ({"condition": "signal", "value": None}, 400),  # a line is no signal
        ({"extra": 1}, 422),
    ],
)
def test_indicator_fields_are_checked(ia, indicator, status):
    assert ia.post("/api/alerts", json=body(**indicator)).status_code == status


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_a_value_that_is_not_finite_is_refused(ia, number):
    raw = (
        '{"instrument": "XAU_USD", "type": "indicator", "timeframe": "1h", "indicator":'
        ' {"uid": "rsi1", "output": "rsi", "condition": "cross_value", "value": NUMBER}}'
    ).replace("NUMBER", number)
    r = ia.post("/api/alerts", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert ok(ia.get("/api/alerts")) == []


def test_a_signal_has_no_direction():
    """A signal needs a marker or a background, and no value or other output."""
    schema = indicator_from_json(
        "sig",
        {
            "name": "Sig",
            "title": "Sig",
            "overlay": True,
            "params": [
                {"name": "c", "type": "color", "default": "#2962ff"},
                {"name": "w", "type": "width", "default": 1, "min": 1, "max": 4},
            ],
            "outputs": [
                {"kind": "line", "name": "line", "label": "Line", "color": "c", "width": "w"},
                {
                    "kind": "marker",
                    "name": "buy",
                    "label": "Buy",
                    "color": "c",
                    "shape": "arrow_up",
                    "position": "below",
                },
                {"kind": "background", "name": "zone", "label": "Zone", "color": "c"},
            ],
        },
    )
    check_condition(schema, "buy", "signal", None, None)
    check_condition(schema, "zone", "signal", None, None)
    check_condition(schema, "line", "cross_value", None, 1.0)
    for args in [
        ("line", "signal", None, None),
        ("buy", "cross_value", None, 1.0),
        ("buy", "signal", None, 1.0),
        ("buy", "signal", "line", None),
        ("line", "cross_output", "zone", None),
    ]:
        with pytest.raises(ConditionError):
            check_condition(schema, *args)


def test_signal_direction_is_refused_on_the_route(ia):
    payload = body() | {"direction": "up"}
    payload["indicator"] = {"uid": "rsi1", "output": "rsi", "condition": "signal"}
    assert ia.post("/api/alerts", json=payload).status_code == 400


def test_warnings_come_from_the_save_time_checks():
    assert warnings_from_checks(None) == ["unchecked"]
    assert warnings_from_checks({"lookahead": "ok", "warmup": "ok"}) == []
    assert warnings_from_checks({"lookahead": "warned", "warmup": "warned"}) == [
        "lookahead",
        "warmup_short",
    ]
    assert warnings_from_checks({"lookahead": "pending", "warmup": "ok"}) == ["unchecked"]
    assert warnings_from_checks({"lookahead": "skipped", "warmup": "skipped"}) == []


def test_edit_rearms_and_checks_again(ia):
    alert = ok(ia.post("/api/alerts", json=body()))
    # As if evaluated: armed, with a last bar.
    live = ia.server.alerts.engine.alerts[alert["id"]]
    live.istate.armed, live.istate.last_bar, live.istate.prev = True, 1_000, -2.0
    renamed = ok(ia.put(f"/api/alerts/{alert['id']}", json={"name": "RSI low"}))
    assert renamed["name"] == "RSI low" and renamed["state"]["armed"] is True  # no re-arm
    for change in (
        {"timeframe": "4h"},
        {"trigger_price": "bid"},
        {"direction": "down"},
        {"indicator": {"uid": "rsi1", "output": "rsi", "condition": "cross_value", "value": 70}},
    ):
        live.istate.armed, live.istate.last_bar = True, 1_000
        edited = ok(ia.put(f"/api/alerts/{alert['id']}", json=change))
        assert edited["state"]["armed"] is False and edited["state"]["last_bar"] is None, change
    assert edited["indicator"]["value"] == 70
    # Another instance is another alert; a bad edit changes nothing.
    other = {"uid": "macd1", "output": "macd", "condition": "cross_value", "value": 0}
    assert ia.put(f"/api/alerts/{alert['id']}", json={"indicator": other}).status_code == 400
    bad = {"uid": "rsi1", "output": "nope", "condition": "cross_value", "value": 1}
    assert ia.put(f"/api/alerts/{alert['id']}", json={"indicator": bad}).status_code == 400
    assert ia.put(f"/api/alerts/{alert['id']}", json={"trigger_mode": "touch"}).status_code == 400
    assert ia.put(f"/api/alerts/{alert['id']}", json={"level": 3.0}).status_code == 400
    assert ia.put(f"/api/alerts/{alert['id']}", json={"timeframe": "10s"}).status_code == 422
    assert ok(ia.get("/api/alerts"))[0]["indicator"]["value"] == 70


def test_stop_and_start_again(ia):
    alert = ok(ia.post("/api/alerts", json=body()))
    stopped = ok(ia.put(f"/api/alerts/{alert['id']}", json={"status": "stopped"}))
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "stopped by you"
    started = ok(ia.put(f"/api/alerts/{alert['id']}", json={"status": "active"}))
    assert started["status"] == "active" and started["stop_reason"] is None
    assert started["state"]["armed"] is False
    # Starting it again checks the instance is still on the chart.
    ok(ia.put(f"/api/alerts/{alert['id']}", json={"status": "stopped"}))
    put_chart(ia, [MACD])
    assert ia.put(f"/api/alerts/{alert['id']}", json={"status": "active"}).status_code == 400
    assert ok(ia.get("/api/alerts"))[0]["status"] == "stopped"


def test_the_state_survives_a_restart_of_the_store(ia):
    alert = ok(ia.post("/api/alerts", json=body()))
    live = ia.server.alerts.engine.alerts[alert["id"]]
    live.istate.armed, live.istate.last_bar, live.istate.prev = True, 1_700_000_000, -1.5
    live.istate.warnings = ["lookahead", "nonsense"]
    call(ia, ia.server.alerts.store.save, live)
    back = call(ia, ia.server.alerts.store.get, alert["id"])
    assert back.istate.last_bar == 1_700_000_000 and back.istate.prev == -1.5
    assert back.istate.armed is True and back.istate.warnings == ["lookahead"]
    assert back.indicator == live.indicator


def test_delete(ia):
    alert = ok(ia.post("/api/alerts", json=body()))
    with ia.websocket_connect("/ws") as ws:
        ok(ia.delete(f"/api/alerts/{alert['id']}"))
        message = receive_until(ws, "alert_changed")
    assert message["op"] == "delete" and message["alert"]["type"] == "indicator"
    assert ok(ia.get("/api/alerts")) == []
    assert ia.server.alerts.engine.alerts == {}


def test_the_line_engine_never_observes_an_indicator_alert(ia):
    from openchart.providers.base import Tick

    alert = ok(ia.post("/api/alerts", json=body()))
    live = ia.server.alerts.engine.alerts[alert["id"]]
    before = (live.state.to_json(), live.istate.to_json())
    call(ia, ia.server.feed.handle_tick, Tick("XAU_USD", 1_791_990_000, 29.0, 29.2))
    call(ia, ia.server.feed.handle_tick, Tick("XAU_USD", 1_791_990_001, 31.0, 31.2))
    assert (live.state.to_json(), live.istate.to_json()) == before
    assert ok(ia.get("/api/alerts/events")) == []


def test_a_line_alert_refuses_an_indicator(ia):
    level = {"instrument": "XAU_USD", "type": "level_cross", "level": 2000.0}
    cond = body()["indicator"]
    assert ia.post("/api/alerts", json={**level, "indicator": cond}).status_code == 400
    created = ok(ia.post("/api/alerts", json=level))
    assert ia.put(f"/api/alerts/{created['id']}", json={"indicator": cond}).status_code == 400


def test_login_and_the_same_origin_check_apply(ia):
    alert = ok(ia.post("/api/alerts", json=body()))
    evil = {"Origin": "https://evil.example"}
    assert ia.post("/api/alerts", json=body(), headers=evil).status_code == 403
    assert ia.put(f"/api/alerts/{alert['id']}", json={"name": "x"}, headers=evil).status_code == 403
    assert ia.delete(f"/api/alerts/{alert['id']}", headers=evil).status_code == 403
    ia.post("/api/logout")
    assert ia.get("/api/alerts").status_code == 401
    assert ia.post("/api/alerts", json=body()).status_code == 401
    assert ia.put(f"/api/alerts/{alert['id']}", json={"name": "x"}).status_code == 401
    assert ia.delete(f"/api/alerts/{alert['id']}").status_code == 401


# "Delete data..." --------------------------------------------------------------------


def test_delete_data_counts_and_removes_indicator_alerts(removed):  # noqa: F811
    client = removed
    put_chart(client, [RSI], instrument=E)
    first = preview(client)
    payload = body() | {"instrument": E}
    alert = ok(client.post("/api/alerts", json=payload))
    level = ok(
        client.post("/api/alerts", json={"instrument": E, "type": "level_cross", "level": 1.1})
    )
    seen = preview(client)
    assert seen["token"] != first["token"]  # a new indicator alert changes the token
    assert seen["stored"]["alerts"] == 2 and seen["stored"]["indicator_alerts"] == 1
    assert seen["stored"]["active_alerts"] == 2 and seen["needs_drawings_and_alerts"] is True
    # The old token is refused; without naming them, the alerts block the delete.
    assert delete(client, first["token"], both=True).status_code == 409
    assert delete(client, seen["token"]).status_code == 409
    # An edit of the indicator alert changes the token too.
    ok(client.put(f"/api/alerts/{alert['id']}", json={"name": "renamed"}))
    assert delete(client, seen["token"], both=True).status_code == 409
    seen = preview(client)
    done = delete(client, seen["token"], both=True)
    assert done.status_code == 200, done.text
    assert done.json()["alerts"] == 2
    assert ok(client.get("/api/alerts")) == []
    assert alert["id"] not in client.server.alerts.engine.alerts
    assert level["id"] not in client.server.alerts.engine.alerts


# A file of yours ---------------------------------------------------------------------


@pytest.fixture
def mine(tmp_path):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from tests.conftest import Harness, run_setup
    from tests.test_custom_indicators import wait_for_worker, write
    from tests.worker_helpers import FakeLauncher

    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    write(folder, "mine.py", "# echoes the closes\n")
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        put_chart(c, [{"uid": "m1", "kind": "custom", "id": "mine", "params": {"width": 3}}])
        yield c


def test_an_alert_on_a_file_of_yours_keeps_its_version_and_shows_it_unchecked(mine):
    payload = body(uid="m1", output="out", value=2000.0)
    alert = ok(mine.post("/api/alerts", json=payload))
    cond = alert["indicator"]
    definition = call(mine, mine.server.custom.get, "mine")
    assert cond["kind"] == "custom" and cond["id"] == "mine"
    assert cond["digest"] == definition.digest and len(cond["digest"]) >= 32
    assert cond["params"] == {}  # only style inputs: nothing that changes its values
    assert alert["state"]["warnings"] == ["unchecked"]  # never computed: no check yet
