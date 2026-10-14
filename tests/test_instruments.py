"""A second and a third instrument (M9): added through the same path as the first, with
nothing but their metadata. EUR_USD is a real one with other decimals and hours than
gold; FAKE_IDX is made up (1 decimal, Berlin hours, a holiday) to prove the generality
without a broker."""

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from openchart.alerts.geometry import line_price
from openchart.market.gaps import find_gaps
from openchart.market.instruments import FX_HOURS, METAL_HOURS
from openchart.market.sessions import TradingClock, TradingHours
from openchart.providers.base import Tick
from openchart.providers.fake import FakeProvider, aggregate_candles, synthetic_m1
from tests.conftest import CANDLES, EURO, GOLD, NOW, ODD, ODD_HOURS
from tests.test_alerts_api import settle, with_telegram


def utc(*a) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp())


NEW_YORK = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
DAYS = 21


def euro_candles():
    return synthetic_m1(NOW - DAYS * 86400, NOW, FX_HOURS, base=1.08)


def odd_candles():
    """FAKE_IDX in its own hours, with a holiday: no data on a weekday 2 weeks ago."""
    candles = synthetic_m1(NOW - DAYS * 86400, NOW, ODD_HOURS, base=18000.0)
    day = datetime.fromtimestamp(NOW - 14 * 86400, BERLIN).date()
    while day.weekday() >= 5:
        day = day.replace(day=day.day - 1)
    holiday = (
        datetime(day.year, day.month, day.day, 9, tzinfo=BERLIN).timestamp(),
        datetime(day.year, day.month, day.day, 17, tzinfo=BERLIN).timestamp(),
    )
    return [c for c in candles if not holiday[0] <= c.ts < holiday[1]], holiday


def add(client, name: str, timeout: float = 20.0) -> None:
    """Settings, Instruments, Add: then wait for its download and closure scan."""
    assert client.post("/api/instruments", json={"instrument": name.lower()}).status_code == 200
    deadline = time.time() + timeout
    while True:
        feed = client.server.feed
        background = feed._background if feed else None
        if background is not None and background.done() and name not in feed.downloading:
            return
        assert time.time() < deadline, f"{name} history did not finish downloading"
        time.sleep(0.05)


@pytest.fixture
def market(client):
    """The fake broker also has EUR_USD and FAKE_IDX, with history for both."""
    euro = euro_candles()
    odd, holiday = odd_candles()
    provider = client.harness.provider
    provider._instruments = [GOLD, EURO, ODD]
    provider.candles.update({"EUR_USD": euro, "FAKE_IDX": odd})
    provider.h1.update({"EUR_USD": aggregate_candles(euro, 3600)})
    client.server.provider_factory = lambda oanda: FakeProvider(instruments=[GOLD, EURO, ODD])
    client.euro, client.odd, client.holiday = euro, odd, holiday
    return client


def instruments(client) -> dict:
    return {i["instrument"]: i for i in client.get("/api/instruments").json()}


def level(instrument: str, price: float) -> dict:
    return {"instrument": instrument, "type": "level_cross", "level": price, "trigger_price": "bid"}


def push(client, instrument: str, t: float, bid: float, spread: float) -> None:
    tick = Tick(instrument, t, bid, bid + spread)
    client.portal.call(client.server.feed.handle_tick, tick)


def test_eurusd_is_added_from_settings_with_its_own_metadata(market):
    client = market
    found = client.get("/api/instruments/available?q=eur").json()
    assert found[0] == {
        "instrument": "EUR_USD",
        "display_name": "EUR/USD",
        "kind": "CURRENCY",
        "enabled": False,
    }
    gold_before = client.get("/api/candles?instrument=XAU_USD&timeframe=1m&limit=5000").json()
    add(client, "EUR_USD")

    eur = instruments(client)["EUR_USD"]
    assert (eur["decimals"], eur["pip_size"], eur["provider_symbol"], eur["display_name"]) == (
        5,
        0.0001,
        "EUR_USD",
        "EUR/USD",
    )
    assert eur["hours"] == FX_HOURS.to_dict() != instruments(client)["XAU_USD"]["hours"]
    assert eur["hours"]["daily_breaks"] == [{"start": "17:00", "end": "17:05"}]
    assert eur["closures"] == []

    # Its history through the normal path: M1, H1 and the bar cache; gold untouched.
    store, feed = client.server.store, client.server.feed
    assert sorted(feed.instruments) == ["EUR_USD", "XAU_USD"]  # one feed, one stream for both
    assert client.portal.call(store.count, "fake", "EUR_USD") == len(market.euro)
    assert client.portal.call(store.count, "fake", "EUR_USD", "H1") > 0
    assert client.portal.call(store.cache_last_ts, "fake", "EUR_USD", "4h") is not None
    assert client.portal.call(store.closures_scanned, "fake", "EUR_USD")
    assert client.portal.call(store.count, "fake", "XAU_USD") == len(CANDLES)
    gold_after = client.get("/api/candles?instrument=XAU_USD&timeframe=1m&limit=5000").json()
    assert gold_after == gold_before

    bars = client.get("/api/candles?instrument=EUR_USD&timeframe=1h&limit=3").json()
    assert bars["decimals"] == 5 and 1.07 < bars["candles"][-1]["c"] < 1.09


def test_eurusd_4h_bars_follow_its_own_hours(market):
    """4h bars open at 17, 21, 01, 05, 09 and 13 New York time. EUR/USD opens on
    Sunday at 17:05 (gold at 18:00), so the weekly open is in the 17:00 bar, and its
    17:00 bars miss only the 5-minute rollover."""
    client = market
    add(client, "EUR_USD")
    bars = client.get("/api/candles?instrument=EUR_USD&timeframe=4h&limit=200").json()["candles"]
    assert len(bars) > 60
    sundays = 0
    for bar in bars:
        local = datetime.fromtimestamp(bar["t"], NEW_YORK)
        assert local.hour in (1, 5, 9, 13, 17, 21) and local.minute == 0
        assert local.weekday() != 5  # nothing on Saturday
        if local.weekday() == 6:  # Sunday 17:00: the weekly open, with data from 17:05
            sundays += 1
            first = next(c for c in market.euro if c.ts >= bar["t"])
            assert datetime.fromtimestamp(first.ts, NEW_YORK).minute == 5 or local.hour == 21
            assert local.hour == 21 or bar["o"] == pytest.approx(first.mid.o)
    assert sundays >= 2
    inside = [b for b in bars if market.euro[0].ts <= b["t"] <= NOW - 4 * 3600]
    for bar in inside:  # weekday bars: 4 open hours, less the rollover at 17:00
        local = datetime.fromtimestamp(bar["t"], NEW_YORK)
        if local.weekday() in (0, 1, 2, 3):
            assert bar["v"] == (235 if local.hour == 17 else 240) * 10


def test_switching_instruments_keeps_each_ones_drawings_and_alerts(market):
    client = market
    add(client, "EUR_USD")
    gold_line = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    eur_line = {"instrument": "EUR_USD", "type": "hline", "points": [{"t": 1, "p": 1.08123}]}
    g = client.post("/api/drawings", json=gold_line).json()
    e = client.post("/api/drawings", json=eur_line).json()
    assert [d["id"] for d in client.get("/api/drawings?instrument=XAU_USD").json()] == [g["id"]]
    assert [d["id"] for d in client.get("/api/drawings?instrument=EUR_USD").json()] == [e["id"]]
    assert client.get("/api/drawings?instrument=EUR_USD").json()[0]["points"][0]["p"] == 1.08123
    body = {"type": "line_cross"}
    a = client.post("/api/alerts", json={**body, "instrument": "EUR_USD", "drawing_id": e["id"]})
    assert a.json()["name"] == "EUR_USD, 1m Crossing horizontal line"
    # A line of one instrument cannot carry an alert of another.
    wrong = {**body, "instrument": "XAU_USD", "drawing_id": e["id"]}
    assert client.post("/api/alerts", json=wrong).status_code == 400


def test_a_sloped_line_alert_on_eurusd_fires_with_eurusd_prices(market):
    client = market
    seen = with_telegram(client)
    add(client, "EUR_USD")
    # A ray falling 0.00100 per trading day, drawn on two weekdays at 10:00 New York.
    day = datetime.fromtimestamp(NOW - 8 * 86400, NEW_YORK).replace(hour=10, minute=0, second=0)
    while day.weekday() not in (0, 1, 2):
        day = day.replace(day=day.day - 1)
    a, b = day.timestamp(), day.timestamp() + 86400
    points = [{"t": a, "p": 1.08500}, {"t": b, "p": 1.08400}]
    ray = client.post(
        "/api/drawings", json={"instrument": "EUR_USD", "type": "ray", "points": points}
    ).json()
    body = {"instrument": "EUR_USD", "type": "line_cross", "drawing_id": ray["id"]}
    eur_alert = client.post("/api/alerts", json={**body, "trigger_price": "bid"}).json()
    gold_alert = client.post("/api/alerts", json=level("XAU_USD", 2000.0)).json()
    eur_level = client.post("/api/alerts", json=level("EUR_USD", 1.2)).json()
    assert eur_level["name"] == "EUR_USD, 1m Crossing 1.20000"  # its own decimals
    client.delete(f"/api/alerts/{eur_level['id']}")

    t1 = time.time()
    t2 = t1 + 1
    line = line_price("ray", points, t2, TradingClock(FX_HOURS))
    push(client, "EUR_USD", t1, line - 0.00050, 0.00012)
    push(client, "EUR_USD", t2, line + 0.00050, 0.00012)  # crossed up
    settle(client)
    [event] = client.get("/api/alerts/events").json()
    assert event["alert_id"] == eur_alert["id"] and event["cross"] == "up"
    assert event["line_value"] == pytest.approx(line, abs=1e-9)
    [text] = [t for t in seen if t.startswith("🔔")]
    assert f"EUR_USD crossed up: {line + 0.0005:.5f} (bid), line {line:.5f}" in text

    # Gold's alert did not move on EUR/USD prices, and still works on gold's own.
    gold = next(x for x in client.get("/api/alerts").json() if x["id"] == gold_alert["id"])
    assert gold["fire_count"] == 0 and gold["status"] == "active"
    push(client, "XAU_USD", t2 + 1, 1999.5, 0.3)
    push(client, "XAU_USD", t2 + 2, 2000.5, 0.3)
    settle(client)
    texts = [t for t in seen if t.startswith("🔔")]
    assert len(texts) == 2 and "XAU_USD crossed up: 2,000.500 (bid), line 2,000.000" in texts[1]


# The third, made-up instrument


def test_a_fake_instrument_with_unusual_metadata_goes_through_the_same_path(market):
    client = market
    seen = with_telegram(client)
    add(client, "EUR_USD")
    add(client, "FAKE_IDX")
    rows = instruments(client)
    odd = rows["FAKE_IDX"]
    assert (odd["decimals"], odd["pip_size"], odd["provider_symbol"], odd["display_name"]) == (
        1,
        1.0,
        "FAKEIDX.cash",
        "Fake Index",
    )
    assert odd["hours"] == ODD_HOURS.to_dict()  # the provider's hours, kept as reported
    assert sorted(client.server.feed.instruments) == ["EUR_USD", "FAKE_IDX", "XAU_USD"]
    assert client.portal.call(client.server.store.count, "fake", "FAKE_IDX") == len(market.odd)

    # The closure scan ran for it, on its own hours: the holiday is learned for it only.
    assert odd["closures"] == [[int(market.holiday[0]), int(market.holiday[1])]]
    assert rows["EUR_USD"]["closures"] == [] and rows["XAU_USD"]["closures"] == []

    # The alert engine's clock for it is its own: stopped over its daily break and holiday.
    clock = client.server.alerts.engine.clocks["FAKE_IDX"]
    night = datetime.fromtimestamp(NOW - 3 * 86400, BERLIN).replace(hour=23, minute=0)
    t = night.timestamp()
    assert clock(t) == clock(t + 3600)  # 23:00-00:00 Berlin: in the break
    h0, h1 = market.holiday
    assert clock(h0) == clock(h1)
    assert TradingClock(FX_HOURS)(t + 3600) > TradingClock(FX_HOURS)(t)  # FX trades then

    # Its prices: 1 decimal on Telegram.
    odd_alert = client.post("/api/alerts", json=level("FAKE_IDX", 18000.0)).json()
    assert odd_alert["name"] == "FAKE_IDX, 1m Crossing 18000.0"
    now = time.time()
    push(client, "FAKE_IDX", now, 17999.6, 1.2)
    push(client, "FAKE_IDX", now + 1, 18000.4, 1.2)
    settle(client)
    [text] = [t for t in seen if t.startswith("🔔")]
    assert "FAKE_IDX crossed up: 18,000.4 (bid), line 18,000.0" in text


def test_a_line_is_straight_across_the_weekend_in_each_instruments_own_hours():
    """The weekend straightness test, for gold, EUR/USD and the fake index: one trading
    day before the weekend continues one trading day after it, whatever the hours."""
    ny = lambda d, h: datetime(2024, 7, d, h, tzinfo=NEW_YORK).timestamp()  # noqa: E731
    berlin = lambda d, h: datetime(2024, 7, d, h, tzinfo=BERLIN).timestamp()  # noqa: E731
    cases = [
        (METAL_HOURS, ny(11, 9), ny(12, 9), ny(15, 9), 23),  # Thu, Fri, Mon 09:00 New York
        (FX_HOURS, ny(11, 9), ny(12, 9), ny(15, 9), 24 - 5 / 60),  # less the rollover
        (ODD_HOURS, berlin(11, 12), berlin(12, 12), berlin(15, 12), 20.75),
    ]
    for hours, thu, fri, monday, day_hours in cases:
        clock = TradingClock(hours)
        assert (clock(fri) - clock(thu)) / 3600 == day_hours  # each one's trading day
        assert (clock(monday) - clock(fri)) / 3600 == day_hours  # the weekend took none
        pts = [{"t": thu, "p": 100.0}, {"t": fri, "p": 99.0}]
        assert line_price("extended", pts, monday, clock) == pytest.approx(98.0)
    # Over gold's daily break the two clocks differ: the hours are per instrument.
    a, b = ny(11, 16), ny(11, 19)
    assert (TradingClock(METAL_HOURS)(b) - TradingClock(METAL_HOURS)(a)) / 3600 == 2
    assert (TradingClock(FX_HOURS)(b) - TradingClock(FX_HOURS)(a)) / 60 == 3 * 60 - 5


def test_the_weekly_open_of_each_instrument_is_its_own():
    sunday = datetime(2024, 7, 14, tzinfo=UTC)
    opens = {}
    for name, hours in (("gold", METAL_HOURS), ("eur", FX_HOURS), ("odd", ODD_HOURS)):
        t = int(sunday.timestamp())
        while not hours.is_open(t):
            t += 60
        opens[name] = t
    assert datetime.fromtimestamp(opens["gold"], NEW_YORK).strftime("%a %H:%M") == "Sun 18:00"
    assert datetime.fromtimestamp(opens["eur"], NEW_YORK).strftime("%a %H:%M") == "Sun 17:05"
    assert datetime.fromtimestamp(opens["odd"], BERLIN).strftime("%a %H:%M") == "Mon 01:15"


# The currency rollover break


def test_a_eurusd_ray_across_17_00_new_york_stays_straight():
    """The 17:00-17:05 rollover is not trading time: the bars on either side (16:59 and
    17:05) are one minute apart on the chart and on the line, like any other two bars."""
    fx = TradingClock(FX_HOURS)

    def ny(h, m):
        return datetime(2024, 7, 10, h, m, tzinfo=NEW_YORK).timestamp()

    pts = [{"t": ny(15, 0), "p": 1.10000}, {"t": ny(16, 0), "p": 1.09940}]  # -0.00001/min

    def at(h, m):
        return line_price("ray", pts, ny(h, m), fx)

    per_minute = at(16, 58) - at(16, 59)
    assert per_minute == pytest.approx(0.00001)
    assert at(16, 59) - at(17, 5) == pytest.approx(per_minute)  # one bar, one step
    assert at(17, 0) == at(17, 3) == at(17, 5)  # flat inside the break: no bars there
    assert at(17, 5) - at(17, 6) == pytest.approx(per_minute)
    # 18:00 is 175 trading minutes after 15:00, not 180.
    assert at(18, 0) == pytest.approx(1.10000 - 175 * 0.00001)


def test_gaps_no_longer_report_the_daily_rollover():
    """OANDA's EUR_USD minutes: nothing from 17:00 to 17:04 New York every weekday.
    With the break in the hours, `gaps` reports none of them."""
    candles = synthetic_m1(utc(2024, 7, 7, 21), utc(2024, 7, 13), FX_HOURS)
    stamps = [c.ts for c in candles]
    assert all(datetime.fromtimestamp(t, NEW_YORK).strftime("%H:%M") != "17:02" for t in stamps)
    assert find_gaps(stamps, FX_HOURS, 2) == []
    no_break = TradingHours("America/New_York", "sun", "17:00", "fri", "17:00")
    rollovers = find_gaps(stamps, no_break, 2)
    assert len(rollovers) == 4  # what `gaps` showed before: Monday to Thursday
    # A real hole of 2+ minutes outside the break is still reported.
    noon = int(datetime(2024, 7, 10, 12, 0, tzinfo=NEW_YORK).timestamp())
    holed = [t for t in stamps if not noon <= t < noon + 180]
    [gap] = find_gaps(holed, FX_HOURS, 2)
    assert gap.open_minutes == 3
