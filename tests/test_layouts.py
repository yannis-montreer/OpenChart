"""Saved layouts and the default layout (SPEC section 11)."""

import pytest

from tests.ws import receive_until

STATE = {
    "instrument": "XAU_USD",
    "timeframe": "1h",
    "chart_type": "heikin_ashi",
    "price": "bid",
    "theme": "light",
    "panel_open": False,
    "panel_view": "alerts",
    "indicators": [{"id": "ema", "params": {"length": 21}}, {"id": "ema", "visible": False}],
}


def test_save_list_load_rename_and_delete(client):
    with client.websocket_connect("/ws") as ws:
        saved = client.post("/api/layouts", json={"name": " Gold 1h ", "layout": STATE}).json()
        assert receive_until(ws, "layouts_changed")["layouts"][0]["id"] == saved["id"]
    assert saved["name"] == "Gold 1h" and saved["default"] is False
    layout = saved["layout"]
    assert {k: layout[k] for k in ("chart_type", "price", "theme", "panel_open", "panel_view")} == {
        "chart_type": "heikin_ashi",
        "price": "bid",
        "theme": "light",
        "panel_open": False,
        "panel_view": "alerts",
    }
    # Indicator parameters are stored complete, so a later default change cannot move them.
    assert layout["indicators"][0]["params"] == {
        "length": 21,
        "source": "close",
        "color": "#2962ff",
        "width": 1,
    }
    assert layout["indicators"][1]["visible"] is False
    assert layout["indicators"][1]["params"]["length"] == 9

    other = client.post("/api/layouts", json={"name": "a scalp", "layout": {}}).json()
    assert other["layout"]["timeframe"] == "15m" and other["layout"]["indicators"] == []
    assert [x["name"] for x in client.get("/api/layouts").json()] == ["a scalp", "Gold 1h"]

    renamed = client.put(f"/api/layouts/{saved['id']}", json={"name": "Gold H1"}).json()
    assert renamed["name"] == "Gold H1" and renamed["layout"] == layout
    changed = client.put(
        f"/api/layouts/{saved['id']}", json={"layout": {**STATE, "chart_type": "line"}}
    ).json()
    assert changed["layout"]["chart_type"] == "line"
    assert client.get(f"/api/layouts/{saved['id']}").json()["layout"]["chart_type"] == "line"

    assert client.delete(f"/api/layouts/{saved['id']}").status_code == 200
    assert client.get(f"/api/layouts/{saved['id']}").status_code == 404
    assert [x["name"] for x in client.get("/api/layouts").json()] == ["a scalp"]


def test_the_default_layout(client):
    a = client.post("/api/layouts", json={"name": "A", "layout": STATE, "default": True}).json()
    b = client.post("/api/layouts", json={"name": "B", "layout": STATE}).json()
    assert a["default"] is True
    defaults = lambda: [x["name"] for x in client.get("/api/layouts").json() if x["default"]]  # noqa: E731
    assert defaults() == ["A"]

    client.put(f"/api/layouts/{b['id']}", json={"default": True})
    assert defaults() == ["B"]  # only one default
    client.put(f"/api/layouts/{a['id']}", json={"default": False})
    assert defaults() == ["B"]  # A was not the default: nothing changes
    client.put(f"/api/layouts/{b['id']}", json={"default": False})
    assert defaults() == []

    client.put(f"/api/layouts/{a['id']}", json={"default": True})
    client.delete(f"/api/layouts/{a['id']}")
    assert defaults() == []
    assert client.get("/api/layouts").json()[0]["name"] == "B"


@pytest.mark.parametrize(
    "change",
    [
        {"chart_type": "renko"},
        {"price": "last"},
        {"timeframe": "7m"},
        {"theme": "blue"},
        {"indicators": [{"id": "nope"}]},
        {"indicators": [{"id": "ema", "params": {"length": -1}}]},
        {"indicators": [{"id": "ema"}] * 31},
    ],
)
def test_validation(client, change):
    out = client.post("/api/layouts", json={"name": "x", "layout": {**STATE, **change}})
    assert out.status_code in (400, 422)
    assert client.post("/api/layouts", json={"name": "", "layout": STATE}).status_code == 422
    assert client.put("/api/layouts/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/layouts/nope").status_code == 404
