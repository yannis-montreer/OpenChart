"""Removing an instrument in Settings hides it and keeps its data (decisions M4): what
the server still says about a hidden instrument, and what Settings needs to add it back."""

from tests.conftest import GOLD
from tests.test_instruments import add, instruments, market  # noqa: F401  (the fixture)


def remove(client, name: str) -> None:
    assert client.delete(f"/api/instruments/{name}").status_code == 200


def test_the_route_keeps_listing_a_removed_instrument_flagged_for_settings(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    assert instruments(client)["EUR_USD"]["enabled"] is True
    remove(client, "EUR_USD")
    rows = instruments(client)
    assert rows["EUR_USD"]["enabled"] is False  # Settings lists it under Removed
    assert rows["XAU_USD"]["enabled"] is True


def test_a_removed_instrument_is_not_in_the_status_or_the_feed(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    assert sorted(client.get("/api/status").json()["instruments"]) == ["EUR_USD", "XAU_USD"]
    remove(client, "EUR_USD")
    assert sorted(client.get("/api/status").json()["instruments"]) == ["XAU_USD"]
    assert sorted(client.server.feed.instruments) == ["XAU_USD"]


def test_a_removed_instrument_keeps_its_data_drawings_and_alerts(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    store = client.server.store
    stored = client.portal.call(store.count, "fake", "EUR_USD")
    line = {"instrument": "EUR_USD", "type": "hline", "points": [{"t": 1, "p": 1.08}]}
    drawing = client.post("/api/drawings", json=line).json()
    alert = client.post(
        "/api/alerts",
        json={"type": "line_cross", "instrument": "EUR_USD", "drawing_id": drawing["id"]},
    ).json()
    remove(client, "EUR_USD")

    assert client.portal.call(store.count, "fake", "EUR_USD") == stored
    assert [d["id"] for d in client.get("/api/drawings?instrument=EUR_USD").json()] == [
        drawing["id"]
    ]
    [kept] = [a for a in client.get("/api/alerts").json() if a["id"] == alert["id"]]
    assert kept["status"] == "active"  # stored, but it cannot fire while the feed is stopped
    assert client.get("/api/candles?instrument=EUR_USD&timeframe=1h&limit=3").status_code == 200


def test_a_removed_instrument_reports_its_stored_size_and_active_alerts(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    line = {"instrument": "EUR_USD", "type": "hline", "points": [{"t": 1, "p": 1.08}]}
    drawing = client.post("/api/drawings", json=line).json()
    client.post(
        "/api/alerts",
        json={"type": "line_cross", "instrument": "EUR_USD", "drawing_id": drawing["id"]},
    )
    stopped = client.post(
        "/api/alerts",
        json={"type": "line_cross", "instrument": "EUR_USD", "drawing_id": drawing["id"]},
    ).json()
    client.put(f"/api/alerts/{stopped['id']}", json={"status": "stopped"})
    assert instruments(client)["EUR_USD"]["stored"] is None  # only removed rows are measured
    remove(client, "EUR_USD")
    stored = instruments(client)["EUR_USD"]["stored"]
    assert stored["bytes"] > 0 and stored["candles"] > 0
    assert (stored["active_alerts"], stored["alerts"], stored["drawings"]) == (1, 2, 1)
    assert instruments(client)["XAU_USD"]["stored"] is None


def test_the_size_is_in_proportion_to_each_instruments_candles(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    remove(client, "EUR_USD")
    remove(client, "XAU_USD")
    rows = instruments(client)
    gold, euro = rows["XAU_USD"]["stored"], rows["EUR_USD"]["stored"]
    assert gold["candles"] != euro["candles"]
    assert (gold["bytes"] > euro["bytes"]) == (gold["candles"] > euro["candles"])


def test_adding_a_removed_instrument_back_keeps_its_data_and_fills_the_gap(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    store = client.server.store
    stored = client.portal.call(store.count, "fake", "EUR_USD")
    remove(client, "EUR_USD")
    assert client.portal.call(store.count, "fake", "EUR_USD") == stored
    add(client, "EUR_USD")
    assert instruments(client)["EUR_USD"]["enabled"] is True
    assert instruments(client)["EUR_USD"]["stored"] is None
    assert client.portal.call(store.count, "fake", "EUR_USD") >= stored
    assert "EUR_USD" in client.get("/api/status").json()["instruments"]


def test_gold_is_unchanged_by_removing_euro(market):  # noqa: F811
    client = market
    add(client, "EUR_USD")
    before = client.get("/api/candles?instrument=XAU_USD&timeframe=1m&limit=5000").json()
    remove(client, "EUR_USD")
    after = client.get("/api/candles?instrument=XAU_USD&timeframe=1m&limit=5000").json()
    assert after == before
    assert GOLD.instrument in client.get("/api/status").json()["instruments"]
