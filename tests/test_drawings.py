"""Drawings: storage, validation, and sync to every open browser (SPEC sections 7 and 10)."""

import pytest

from tests.conftest import NOW

RAY = {
    "instrument": "XAU_USD",
    "type": "ray",
    "points": [{"t": NOW - 7200, "p": 2010.5}, {"t": NOW - 3600, "p": 2005.25}],
    "style": {"color": "#22c55e", "width": 3},
}


def receive_until(ws, kind: str, limit: int = 50) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind} message")


def test_create_list_update_delete(client):
    created = client.post("/api/drawings", json=RAY).json()
    assert created["type"] == "ray" and len(created["id"]) == 16
    assert created["style"] == {"color": "#22c55e", "width": 3, "dash": "solid"}
    assert created["points"] == RAY["points"]

    listed = client.get("/api/drawings", params={"instrument": "XAU_USD"}).json()
    assert [d["id"] for d in listed] == [created["id"]]
    assert client.get("/api/drawings", params={"instrument": "EUR_USD"}).json() == []

    moved = [{"t": NOW - 7200, "p": 2011.0}, {"t": NOW - 1800, "p": 2001.0}]
    updated = client.put(
        f"/api/drawings/{created['id']}",
        json={"points": moved, "style": {"dash": "dashed"}, "label": "Downtrend"},
    ).json()
    assert updated["points"] == moved
    assert updated["style"] == {"color": "#22c55e", "width": 3, "dash": "dashed"}
    assert updated["label"] == "Downtrend"
    cleared = client.put(f"/api/drawings/{created['id']}", json={"clear_label": True}).json()
    assert cleared["label"] is None

    assert client.delete(f"/api/drawings/{created['id']}").status_code == 200
    assert client.get("/api/drawings").json() == []
    assert client.delete(f"/api/drawings/{created['id']}").status_code == 404


def test_horizontal_line_has_one_point(client):
    hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": NOW, "p": 2000.0}]}
    assert client.post("/api/drawings", json=hline).status_code == 200
    two = {**hline, "points": RAY["points"]}
    assert client.post("/api/drawings", json=two).status_code == 400


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"type": "circle"}, 422),
        ({"points": [{"t": NOW, "p": 1.0}]}, 400),  # a ray needs two points
        ({"points": [{"t": NOW, "p": 1.0}, {"t": NOW, "p": 2.0}]}, 400),  # same time
        ({"style": {"color": "red"}}, 422),
        ({"style": {"width": 9}}, 422),
        ({"style": {"dash": "wavy"}}, 422),
        ({"label": "x" * 101}, 422),
        ({"instrument": "NOPE"}, 404),
    ],
)
def test_validation(client, change, status):
    assert client.post("/api/drawings", json={**RAY, **change}).status_code == status


def test_update_checks_point_count(client):
    created = client.post("/api/drawings", json=RAY).json()
    response = client.put(f"/api/drawings/{created['id']}", json={"points": [{"t": NOW, "p": 1}]})
    assert response.status_code == 400
    assert client.put("/api/drawings/nope", json={}).status_code == 404


def test_every_open_browser_receives_changes(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        created = client.post("/api/drawings", json=RAY).json()
        for ws in (a, b):
            message = receive_until(ws, "drawing_changed")
            assert message["op"] == "create" and message["drawing"] == created

        client.put(f"/api/drawings/{created['id']}", json={"style": {"color": "#ff0000"}})
        for ws in (a, b):
            message = receive_until(ws, "drawing_changed")
            assert message["op"] == "update"
            assert message["drawing"]["style"]["color"] == "#ff0000"

        client.delete(f"/api/drawings/{created['id']}")
        for ws in (a, b):
            message = receive_until(ws, "drawing_changed")
            assert message["op"] == "delete" and message["drawing"]["id"] == created["id"]


def test_delete_all_for_an_instrument(client):
    for _ in range(3):
        client.post("/api/drawings", json=RAY)
    with client.websocket_connect("/ws") as ws:
        out = client.delete("/api/drawings", params={"instrument": "XAU_USD"}).json()
        assert out == {"deleted": 3}
        ops = [receive_until(ws, "drawing_changed")["op"] for _ in range(3)]
        assert ops == ["delete"] * 3
    assert client.get("/api/drawings").json() == []


def test_drawings_survive_a_restart(harness):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from openchart.server import Server
    from tests.conftest import PASSWORD, run_setup

    harness.seed_candles()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        created = c.post("/api/drawings", json=RAY).json()

    again = Server(harness.config, provider=harness.provider, secrets=harness.secrets)
    with TestClient(create_app(again)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        assert c.get("/api/drawings").json()[0]["id"] == created["id"]


def test_drawings_need_a_login(fresh):
    assert fresh.get("/api/drawings").status_code == 401
    assert fresh.post("/api/drawings", json=RAY).status_code == 401


def test_two_points_inside_one_closure_are_refused(client):
    from datetime import UTC, datetime

    def utc(*a):
        return int(datetime(*a, tzinfo=UTC).timestamp())

    # Saturday and Sunday morning: no gold trading between them.
    closed = [{"t": utc(2024, 7, 13, 12), "p": 1.0}, {"t": utc(2024, 7, 14, 12), "p": 2.0}]
    response = client.post("/api/drawings", json={**RAY, "points": closed})
    assert response.status_code == 400
    assert "trading time" in response.json()["detail"]
