"""The new drawing tools (vertical line, rectangle, channel, Fibonacci, text, measure):
the shape rules, the routes and the alerts that must not apply to them yet."""

import json
from pathlib import Path

import pytest

from openchart.drawing_rules import TYPES, channel_offset, shape_error
from tests.conftest import NOW
from tests.ws import receive_until

FIXTURE = Path(__file__).resolve().parent.parent / "frontend/src/drawings/shape-rules.fixture.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def _clock(closure):
    """Identity, except that time inside `closure` [lo, hi] does not count."""
    if not closure:
        return lambda t: float(t)
    lo, hi = closure
    return lambda t: float(t - max(0, min(t, hi) - lo))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shape_rules_match_the_shared_fixture(case):
    points = [{"t": t, "p": p} for t, p in case["points"]]
    error = shape_error(case["type"], points, _clock(case.get("closure")), case.get("label"))
    assert (error is None) is case["ok"], error


def test_point_counts():
    assert TYPES == {
        "hline": 1,
        "trendline": 2,
        "ray": 2,
        "extended": 2,
        "vline": 1,
        "text": 1,
        "rect": 2,
        "fib": 2,
        "measure": 2,
        "channel": 3,
    }


def test_channel_offset_is_measured_on_the_extended_base_line():
    points = [{"t": 0, "p": 0}, {"t": 100, "p": 10}, {"t": 50, "p": 8}]
    assert channel_offset(points, _clock(None)) == pytest.approx(3.0)
    outside = [{"t": 0, "p": 0}, {"t": 100, "p": 10}, {"t": 300, "p": 25}]
    assert channel_offset(outside, _clock(None)) == pytest.approx(-5.0)


def test_channel_offset_follows_trading_time_not_clock_time():
    # Time between 100 and 200 does not count: at t=250 the base (0,0)-(300,15) is at
    # trading time 150 of 200, i.e. 11.25, not 12.5.
    clock = _clock([100, 200])
    points = [{"t": 0, "p": 0}, {"t": 300, "p": 15}, {"t": 250, "p": 13.25}]
    assert channel_offset(points, clock) == pytest.approx(2.0)


def pt(t, p):
    return {"t": NOW + t, "p": p}


NEW = {
    "vline": [pt(-3600, 2000.0)],
    "text": [pt(-3600, 2000.0)],
    "rect": [pt(-7200, 2010.0), pt(-3600, 2000.0)],
    "fib": [pt(-7200, 2010.0), pt(-3600, 2000.0)],
    "measure": [pt(-7200, 2010.0), pt(-3600, 2000.0)],
    "channel": [pt(-7200, 2000.0), pt(-3600, 2010.0), pt(-5400, 2012.0)],
}


@pytest.mark.parametrize("kind", NEW)
def test_every_new_type_is_stored_listed_moved_and_deleted(client, kind):
    body = {"instrument": "XAU_USD", "type": kind, "points": NEW[kind]}
    if kind == "text":
        body["label"] = "Support"
    created = client.post("/api/drawings", json=body)
    assert created.status_code == 200, created.text
    drawing = created.json()
    assert drawing["type"] == kind and drawing["points"] == NEW[kind]
    listed = client.get("/api/drawings", params={"instrument": "XAU_USD"}).json()
    assert [d["id"] for d in listed] == [drawing["id"]]
    moved = [{"t": p["t"] + 60, "p": p["p"] + 1} for p in NEW[kind]]
    updated = client.put(f"/api/drawings/{drawing['id']}", json={"points": moved})
    assert updated.status_code == 200 and updated.json()["points"] == moved
    assert client.delete(f"/api/drawings/{drawing['id']}").status_code == 200


@pytest.mark.parametrize("kind", NEW)
def test_a_wrong_point_count_is_refused(client, kind):
    points = NEW[kind][:-1] if len(NEW[kind]) > 1 else NEW[kind] * 2
    body = {"instrument": "XAU_USD", "type": kind, "points": points, "label": "x"}
    assert client.post("/api/drawings", json=body).status_code == 400


def test_rules_are_checked_again_on_update(client):
    body = {"instrument": "XAU_USD", "type": "rect", "points": NEW["rect"]}
    drawing = client.post("/api/drawings", json=body).json()
    flat = [pt(-7200, 2000.0), pt(-3600, 2000.0)]
    assert client.put(f"/api/drawings/{drawing['id']}", json={"points": flat}).status_code == 400
    third = {"instrument": "XAU_USD", "type": "channel", "points": NEW["channel"]}
    channel = client.post("/api/drawings", json=third).json()
    on_line = [*NEW["channel"][:2], pt(-5400, 2005.0)]
    assert client.put(f"/api/drawings/{channel['id']}", json={"points": on_line}).status_code == 400


def test_text_needs_its_text_and_keeps_it(client):
    body = {"instrument": "XAU_USD", "type": "text", "points": NEW["text"]}
    assert client.post("/api/drawings", json=body).status_code == 400
    assert client.post("/api/drawings", json={**body, "label": "  "}).status_code == 400
    note = client.post("/api/drawings", json={**body, "label": "Support"}).json()
    assert client.put(f"/api/drawings/{note['id']}", json={"clear_label": True}).status_code == 400
    blank = client.put(f"/api/drawings/{note['id']}", json={"label": "   "})
    assert blank.status_code == 400
    changed = client.put(f"/api/drawings/{note['id']}", json={"label": "Resistance"}).json()
    assert changed["label"] == "Resistance"


def test_fill_and_extend_are_stored_only_when_given_and_validated(client):
    body = {"instrument": "XAU_USD", "type": "rect", "points": NEW["rect"]}
    plain = client.post("/api/drawings", json=body).json()
    assert "fill" not in plain["style"] and "extend" not in plain["style"]
    styled = client.post(
        "/api/drawings", json={**body, "style": {"fill": 30, "extend": True}}
    ).json()
    assert styled["style"]["fill"] == 30 and styled["style"]["extend"] is True
    again = client.put(f"/api/drawings/{styled['id']}", json={"style": {"color": "#ff0000"}}).json()
    assert again["style"]["fill"] == 30 and again["style"]["color"] == "#ff0000"
    for bad in ({"fill": 101}, {"fill": -1}, {"fill": "30"}, {"fill": 30.5}, {"extend": "yes"}):
        assert client.post("/api/drawings", json={**body, "style": bad}).status_code == 422


@pytest.mark.parametrize("kind", NEW)
def test_a_line_alert_cannot_be_put_on_a_new_type_yet(client, kind):
    body = {"instrument": "XAU_USD", "type": kind, "points": NEW[kind], "label": "x"}
    drawing = client.post("/api/drawings", json=body).json()
    alert = client.post(
        "/api/alerts",
        json={"instrument": "XAU_USD", "type": "line_cross", "drawing_id": drawing["id"]},
    )
    assert alert.status_code == 400
    assert "line" in alert.json()["detail"]


def test_a_line_alert_still_works_on_a_line(client):
    line = {"instrument": "XAU_USD", "type": "hline", "points": [pt(0, 2000.0)]}
    drawing = client.post("/api/drawings", json=line).json()
    alert = client.post(
        "/api/alerts",
        json={"instrument": "XAU_USD", "type": "line_cross", "drawing_id": drawing["id"]},
    )
    assert alert.status_code == 200


def test_new_drawings_reach_every_browser(client):
    with client.websocket_connect("/ws") as ws:
        body = {"instrument": "XAU_USD", "type": "fib", "points": NEW["fib"]}
        created = client.post("/api/drawings", json=body).json()
        message = receive_until(ws, "drawing_changed")
        assert message["op"] == "create" and message["drawing"]["id"] == created["id"]
        assert message["drawing"]["type"] == "fib"
