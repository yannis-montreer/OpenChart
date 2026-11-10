"""The gaps left by the indicator alert engine (docs/decisions-indicator-alert-gaps.md):

1. deleting a definition counts the indicator alerts on it, refuses unless they are named,
   and is all or nothing (an injected failure half-way leaves no alert stopped);
2. an alert that cannot be evaluated (an incomplete condition; its indicator, output or
   instance gone) shows an error with its reason, is reported once, and is never skipped
   silently;
3. applying a layout keeps the chart's uids for matched instances, previews the alerts it
   would stop, and refuses unless they are named;
4. the crossing rule per output kind: on a histogram, a marker or a background, a bar at
   exactly 0 is not a side, so (-1, 0, -1) never fires;
5. live and replay decide identically, also for a value within 1e-9 of the threshold, and
   the event log keeps the evaluated value of each fire.
"""

from __future__ import annotations

import itertools
import json
import random
import sqlite3

import pandas as pd
import pytest

from openchart.alerts import indicator_eval as ie
from openchart.indicators._custom import indicator_from_json
from openchart.indicators._registry import load_indicators
from openchart.store.alerts import AlertStore, IndicatorCondition
from openchart.store.sqlite import Database, atomic
from tests.bounded import call
from tests.test_chart_indicators import allow_editing
from tests.test_indicator_alerts_server import LAST, alert_body, ok, put_chart, run, wk  # noqa: F401
from tests.test_indicator_eval import Harness, World, store  # noqa: F401

# Helpers --------------------------------------------------------------------------------


def deny(c, sql_action: int, table: str):
    """Make the database refuse one kind of statement on one table, from now on: an
    injected failure in the middle of a write, without touching the code under test."""

    def authorizer(action, arg1, *_):
        if action == sql_action and arg1 == table:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    call(c, atomic, c.server.db.conn, lambda db: db.set_authorizer(authorizer))


def allow_all(c):
    call(c, atomic, c.server.db.conn, lambda db: db.set_authorizer(None))


def chart_uids(c, instrument="XAU_USD"):
    url = f"/api/charts/{c.server.source}/{instrument}/indicators"
    return [(i["uid"], i["id"]) for i in ok(c.get(url))["indicators"]]


def alerts_by_id(c):
    return {a["id"]: a for a in ok(c.get("/api/alerts"))}


def stop_messages(c):
    return [m for m in c.sent if "stopped" in m]


def delete_url(ids=(), confirm=True):
    q = ["confirm=true"] if confirm else []
    q += [f"alerts={i}" for i in ids]
    return "/api/custom-indicators/mine?" + "&".join(q)


# 1. Deleting a definition -----------------------------------------------------------------


def test_deleting_a_definition_counts_its_alerts_and_needs_them_named(wk):  # noqa: F811
    allow_editing(wk)
    a1 = ok(wk.post("/api/alerts", json=alert_body()))
    a2 = ok(wk.post("/api/alerts", json=alert_body(value=1.0)))
    ok(wk.put(f"/api/alerts/{a2['id']}", json={"status": "stopped"}))  # stopped ones count too

    usage = ok(wk.get("/api/custom-indicators/mine/usage"))
    assert {a["id"] for a in usage["alerts"]} == {a1["id"], a2["id"]}
    assert usage["in_use"] is True

    for url in (
        delete_url(confirm=False),
        delete_url(),  # confirmed, but the alerts are not named
        delete_url([a1["id"]]),  # one missing
        delete_url([a1["id"], a2["id"], "notanalert"]),  # one too many
    ):
        r = wk.delete(url)
        assert r.status_code == 409, (url, r.text)
        detail = r.json()["detail"]
        assert "2 indicator alerts" in detail["message"]
        assert {a["id"] for a in detail["usage"]["alerts"]} == {a1["id"], a2["id"]}
    assert (wk.folder / "mine.py").is_file()
    assert alerts_by_id(wk)[a1["id"]]["status"] == "active"

    body = ok(wk.delete(delete_url([a2["id"], a1["id"]])))
    assert {a["id"] for a in body["removed"]["alerts"]} == {a1["id"], a2["id"]}
    after = alerts_by_id(wk)
    assert after[a1["id"]]["status"] == "stopped"
    assert "deleted" in after[a1["id"]]["stop_reason"]
    assert after[a2["id"]]["stop_reason"] == "stopped by you"  # already stopped: kept as it was
    assert wk.server.alerts.engine.alerts[a1["id"]].status == "stopped"
    assert len(stop_messages(wk)) == 1 and "deleted" in stop_messages(wk)[0]


def test_a_definition_whose_only_use_is_an_alert_still_needs_it_named(wk):  # noqa: F811
    allow_editing(wk)
    a1 = ok(wk.post("/api/alerts", json=alert_body()))
    put_chart(wk, [])  # the instance goes; the (stopped) alert stays on the definition
    r = wk.delete("/api/custom-indicators/mine")
    assert r.status_code == 409 and "1 indicator alert" in r.json()["detail"]["message"]
    ok(wk.delete(delete_url([a1["id"]])))
    assert not (wk.folder / "mine.py").exists()


@pytest.mark.parametrize("refused", ["layouts", "alerts"])
def test_a_delete_that_fails_half_way_leaves_no_alert_stopped(wk, refused):  # noqa: F811
    """The database refuses one write in the middle of the delete (the layout's, after
    the charts; or the alerts' stop, after the charts and the layout): nothing of it may
    stay."""
    allow_editing(wk)
    from openchart.market.instruments import FX_HOURS
    from tests.conftest import EURO

    wk.portal.call(wk.server.store.upsert_instrument, wk.server.source, EURO, FX_HOURS)
    put_chart(wk, [{"uid": "e9", "kind": "custom", "id": "mine", "params": {}}], "EUR_USD")
    layout = ok(wk.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    a1 = ok(wk.post("/api/alerts", json=alert_body()))
    a2 = ok(wk.post("/api/alerts", json=alert_body(value=1.0)))
    run(wk, LAST.ts + 65)  # armed
    before = (chart_uids(wk), chart_uids(wk, "EUR_USD"))
    sent = len(wk.sent)

    deny(wk, sqlite3.SQLITE_UPDATE, refused)
    try:
        with pytest.raises(Exception, match="not authorized"):
            wk.delete(delete_url([a1["id"], a2["id"]]))
    finally:
        allow_all(wk)

    assert (wk.folder / "mine.py").is_file()
    assert (chart_uids(wk), chart_uids(wk, "EUR_USD")) == before
    kept = ok(wk.get(f"/api/layouts/{layout['id']}"))["layout"]["indicators"]
    assert [i["id"] for i in kept] == ["mine"]
    for a in (a1, a2):
        got = alerts_by_id(wk)[a["id"]]
        assert got["status"] == "active" and got["stop_reason"] is None
        live = wk.server.alerts.engine.alerts[a["id"]]
        assert live.status == "active" and live.istate.armed  # in the engine too
        stored = call(wk, wk.server.alerts.store.get, a["id"])
        assert stored.status == "active" and stored.stop_reason is None
    assert wk.sent[sent:] == []  # nothing was announced
    # And it works once the database does.
    ok(wk.delete(delete_url([a1["id"], a2["id"]])))
    assert alerts_by_id(wk)[a1["id"]]["status"] == "stopped"


# 2. Alerts that cannot be evaluated ------------------------------------------------------


async def broken_alert(h, condition_json, name="Broken one"):
    """An active indicator alert whose stored condition is `condition_json` (as written by
    hand or by an older version), loaded as the server loads it."""
    alert = await h.add()
    alert.name = name
    await h.store.save(alert)
    await h.store.conn.execute(
        "UPDATE alerts SET condition_json=? WHERE id=?", (condition_json, alert.id)
    )
    await h.store.conn.commit()
    loaded = await h.store.get(alert.id)
    h.alerts[alert.id] = loaded
    return loaded


async def run_and_watch(h, w, bars):
    msgs = []
    for i in bars:
        await h.close(i)
        msgs += h.ev.watch(w.at_close(i))
    return msgs


def about(msgs, name):
    return [m for m in msgs if name in m]


@pytest.mark.parametrize(
    "condition_json",
    [None, json.dumps({"uid": "u1", "kind": "custom"}), json.dumps(["not", "an", "object"])],
)
async def test_an_incomplete_condition_is_an_error_reported_once(store, condition_json):  # noqa: F811
    w = World(closes=[-1, 1, -1, 1, -1, 1])
    h = Harness(store, w)
    alert = await broken_alert(h, condition_json)
    assert alert.indicator is None
    msgs = await run_and_watch(h, w, range(6))
    assert "incomplete" in alert.istate.error
    assert alert.to_json()["state"]["error"] == alert.istate.error  # what the API shows
    assert alert.status == "active" and h.fires == []
    assert "incomplete" in (await store.get(alert.id)).istate.error
    assert len(about(msgs + h.messages, "Broken one")) == 1


async def test_a_condition_that_no_longer_fits_its_output_is_an_error(store):  # noqa: F811
    """The definition lost the output (a built-in changed by an upgrade, the same version
    string): the result may even still carry it."""
    w = World(closes=[-1, 1, -1, 1, -1, 1])
    w.answer = lambda frame, out: {**out, "gone": out["out"]}
    h = Harness(store, w)
    cond = {
        **IndicatorCondition("u1", "custom", "echo", {}, "v1", "gone", "signal").to_json(),
    }
    alert = await broken_alert(h, json.dumps(cond))
    assert alert.indicator is not None
    msgs = await run_and_watch(h, w, range(6))
    assert "no output 'gone'" in alert.istate.error
    assert alert.to_json()["state"]["error"] == alert.istate.error
    assert h.fires == [] and alert.istate.last_bar is None
    assert len(about(msgs + h.messages, "Broken one")) == 1


async def test_an_indicator_that_no_longer_exists_is_an_error_reported_once(store):  # noqa: F811
    w = World(closes=[-1, 1, -1, 1, -1, 1])
    h = Harness(store, w)
    alert = await h.add(kind="builtin", ind="no_such_indicator", output="x")
    alert.name = "Broken one"
    msgs = await run_and_watch(h, w, range(6))
    assert "no longer exists" in alert.istate.error
    assert "no_such_indicator" in alert.istate.error
    assert alert.to_json()["state"]["error"] == alert.istate.error
    assert (await store.get(alert.id)).istate.error == alert.istate.error
    assert len(about(msgs + h.messages, "Broken one")) == 1


async def test_an_instance_no_longer_on_the_chart_is_an_error_reported_once(store):  # noqa: F811
    w = World(closes=[-1, 1, -1, 1, -1, 1])
    h = Harness(store, w)
    alert = await h.add()
    other = await h.add(value=0.5)
    alert.name, other.name = "Broken one", "Fine one"
    on_chart = {"u1"}

    async def instances(instrument):
        assert instrument == "XAU_USD"
        return set(on_chart)

    h.ev.instances = instances
    msgs = await run_and_watch(h, w, range(2))
    assert alert.istate.error is None and alert.istate.armed
    fired = len(h.fires)
    on_chart.clear()  # the row went without a chart write (an older version, by hand)
    msgs += await run_and_watch(h, w, range(2, 6))
    assert "no longer on the XAU_USD chart" in alert.istate.error
    assert alert.to_json()["state"]["error"] == alert.istate.error
    assert alert.istate.last_bar == w.starts[1] and len(h.fires) == fired
    assert len(about(msgs + h.messages, "Broken one")) == 1
    assert other.istate.error is not None  # the same instance: the same error


def test_the_api_shows_why_an_alert_cannot_be_evaluated(wk):  # noqa: F811
    a1 = ok(wk.post("/api/alerts", json=alert_body()))
    run(wk, LAST.ts + 65)
    call(wk, wk.server.db.conn.execute, "DELETE FROM chart_indicators")
    call(wk, wk.server.db.conn.commit)
    run(wk, LAST.ts + 125)
    got = alerts_by_id(wk)[a1["id"]]
    assert got["status"] == "active"
    assert "no longer on the XAU_USD chart" in got["state"]["error"]
    wk.portal.call(watch, wk)
    wk.portal.call(watch, wk)
    assert len([m for m in wk.sent if "no longer on the XAU_USD chart" in m]) == 1


# 3. Applying a layout ---------------------------------------------------------------------


def save_layout(c, name, **layout):
    return ok(c.post("/api/layouts", json={"name": name, "layout": layout}))["id"]


def preview(c, layout_id, instrument="XAU_USD"):
    return ok(c.get(f"/api/layouts/{layout_id}/apply-preview?instrument={instrument}"))


def apply(c, layout_id, stop=(), instrument="XAU_USD"):
    q = "".join(f"&stop_alerts={i}" for i in stop)
    return c.post(f"/api/layouts/{layout_id}/apply?instrument={instrument}{q}")


SMA5 = {"kind": "builtin", "id": "sma", "params": {"length": 5}}


def sma_alert(c, uid):
    return ok(c.post("/api/alerts", json=alert_body(uid=uid, output="sma")))


def test_the_same_indicator_from_another_layout_keeps_the_charts_uid_and_alert(wk):  # noqa: F811
    put_chart(wk, [{"uid": "a1", **SMA5}])
    a = sma_alert(wk, "a1")
    run(wk, LAST.ts + 65)
    layout = save_layout(wk, "Mine", indicators=[{"uid": "L1", **SMA5}])
    p = preview(wk, layout)
    assert p["alerts_stopped"] == [] and p["alerts_rearmed"] == []
    body = ok(apply(wk, layout))
    assert [i["uid"] for i in body["chart"]["indicators"]] == ["a1"]
    assert body["alerts_stopped"] == []
    live = wk.server.alerts.engine.alerts[a["id"]]
    assert live.status == "active" and live.istate.armed  # still armed: nothing changed


def test_matching_is_by_uid_first_then_definition_and_values_in_legend_order(wk):  # noqa: F811
    put_chart(
        wk,
        [
            {"uid": "a1", **SMA5},
            {"uid": "a2", **SMA5},
            {"uid": "a3", "kind": "builtin", "id": "sma", "params": {"length": 9}},
        ],
    )
    layout = save_layout(
        wk,
        "Mine",
        indicators=[
            {"uid": "L1", **SMA5},  # same values as a1 and a2: the first unmatched one
            {"uid": "a2", **SMA5},  # a2 by uid
            {"uid": "L3", "kind": "builtin", "id": "sma", "params": {"length": 20}},
        ],
    )
    body = ok(apply(wk, layout, stop=[]))
    # a3 has other values: not matched, removed; L3 is new.
    assert [i["uid"] for i in body["chart"]["indicators"]] == ["a1", "a2", "L3"]


def test_a_uid_shared_with_another_definition_is_not_a_match(wk):  # noqa: F811
    put_chart(wk, [{"uid": "x1", **SMA5}])
    a = sma_alert(wk, "x1")
    layout = save_layout(wk, "Mine", indicators=[{"uid": "x1", "kind": "builtin", "id": "ema"}])
    p = preview(wk, layout)
    assert [x["id"] for x in p["alerts_stopped"]] == [a["id"]]
    body = ok(apply(wk, layout, stop=[a["id"]]))
    [(uid, ind)] = [(i["uid"], i["id"]) for i in body["chart"]["indicators"]]
    assert ind == "ema" and uid != "x1"  # never takes over the alert's instance
    assert alerts_by_id(wk)[a["id"]]["status"] == "stopped"


def test_an_apply_that_would_stop_alerts_is_previewed_and_needs_them_named(wk):  # noqa: F811
    put_chart(wk, [{"uid": "a1", **SMA5}, {"uid": "a2", "kind": "builtin", "id": "rsi"}])
    kept = sma_alert(wk, "a1")
    lost = ok(
        wk.post(
            "/api/alerts",
            json=alert_body(uid="a2", output="rsi", value=50.0) | {"name": "RSI fifty"},
        )
    )
    changed = ok(
        wk.post(
            "/api/alerts",
            json=alert_body(uid="a1", output="sma", value=2.0) | {"name": "other"},
        )
    )
    run(wk, LAST.ts + 65)
    layout = save_layout(
        wk, "Only SMA", indicators=[{"uid": "a1", "kind": "builtin", "id": "sma", "params": {}}]
    )
    before = chart_uids(wk)
    p = preview(wk, layout)
    assert [x["id"] for x in p["alerts_stopped"]] == [lost["id"]]
    assert p["alerts_stopped"][0]["name"] == "RSI fifty"
    assert {x["id"] for x in p["alerts_rearmed"]} == {kept["id"], changed["id"]}
    assert chart_uids(wk) == before  # a preview changes nothing

    for named in ([], [kept["id"]], [lost["id"], kept["id"]]):
        r = apply(wk, layout, stop=named)
        assert r.status_code == 409, r.text
        assert [x["id"] for x in r.json()["detail"]["preview"]["alerts_stopped"]] == [lost["id"]]
    assert chart_uids(wk) == before
    assert alerts_by_id(wk)[lost["id"]]["status"] == "active"

    body = ok(apply(wk, layout, stop=[lost["id"]]))
    assert [x["id"] for x in body["alerts_stopped"]] == [lost["id"]]
    got = alerts_by_id(wk)
    assert got[lost["id"]]["status"] == "stopped"
    assert "Only SMA" in got[lost["id"]]["stop_reason"]
    assert got[kept["id"]]["status"] == "active" and got[kept["id"]]["state"]["armed"] is False
    assert len(stop_messages(wk)) == 1 and "RSI fifty" in stop_messages(wk)[0]


# 4. The crossing rule per output kind -----------------------------------------------------

KINDS_SCHEMA = indicator_from_json(
    "echo",
    {
        "name": "Echo",
        "title": "Echo",
        "overlay": False,
        "params": [
            {"name": "c", "type": "color", "default": "#2962ff"},
            {"name": "w", "type": "width", "default": 1, "min": 1, "max": 4},
        ],
        "outputs": [
            {"kind": "line", "name": "out", "label": "Out", "color": "c", "width": "w"},
            {"kind": "histogram", "name": "hist", "label": "Hist", "color": "c"},
            {"kind": "area", "name": "area", "label": "Area", "color": "c"},
            {"kind": "marker", "name": "buy", "label": "Buy", "color": "c"},
            {"kind": "background", "name": "zone", "label": "Zone", "color": "c"},
        ],
    },
)


class KindsWorld(World):
    """Every output is the bar's close as it is: -1, 0 and -1 reach each kind raw."""

    async def resolve(self, kind, indicator_id):
        return ie.Resolved(KINDS_SCHEMA, self.digest, "ok")

    async def compute(self, key, schema, settings, frame):
        self.computes += 1
        c = pd.Series(frame["c"].to_numpy(dtype=float))
        return {"out": c, "hist": c, "area": c, "buy": c, "zone": c}


async def fires_on(store, closes, output, condition="cross_value"):  # noqa: F811
    w = KindsWorld(count=len(closes) + 1, closes=[closes[0], *closes])  # arms on the first
    h = Harness(store, w)
    await h.add(condition=condition, output=output, value=None if condition == "signal" else 0.0)
    for i in range(len(w.starts)):
        await h.close(i)
    return [(w.starts.index(f.bar) - 1, f.cross) for f in h.fires]


async def test_a_histogram_touching_zero_and_going_back_is_no_cross(store):  # noqa: F811
    assert await fires_on(store, [-1, 0, -1], "hist") == []
    assert await fires_on(store, [1, 0, 1], "hist") == []


async def test_a_histogram_leaving_zero_to_the_other_side_crosses_once(store):  # noqa: F811
    assert await fires_on(store, [-1, 0, 1], "hist") == [(2, "up")]
    assert await fires_on(store, [1, 0, 0, -1], "hist") == [(3, "down")]
    assert await fires_on(store, [-1, 1], "hist") == [(1, "up")]


@pytest.mark.parametrize("output", ["out", "area"])
async def test_lines_and_areas_keep_the_line_rule(store, output):  # noqa: F811
    """Unchanged (as a drawn line): touching then leaving the value is a cross."""
    assert await fires_on(store, [-1, 0, -1], output) == [(2, "down")]


@pytest.mark.parametrize("output", ["buy", "zone"])
async def test_markers_and_backgrounds_at_minus_one_zero_minus_one_never_fire(store, output):  # noqa: F811
    assert await fires_on(store, [-1, 0, -1], output, "signal") == []


async def test_a_background_turns_on_from_any_value_that_is_not_on(store):  # noqa: F811
    assert await fires_on(store, [-1, 1, 1, 0, 1], "zone", "signal") == [
        (1, "signal"),
        (4, "signal"),
    ]
    assert await fires_on(store, [-1, 1, 1, 0, 1], "buy", "signal") == [
        (1, "signal"),
        (2, "signal"),
        (4, "signal"),
    ]


def test_the_rule_per_kind():
    c = ie.crossing
    assert c("cross_value", "histogram", 0.0, 1.0) is None  # from 0: no side yet
    assert c("cross_value", "histogram", -1.0, 1.0) == "up"
    assert c("cross_value", "line", 0.0, 1.0) == "up"  # the line rule
    assert c("cross_value", None, 0.0, -1.0) == "down"
    assert c("signal", "marker", 0.0, -1.0) is None


# 5. Live and replay decide identically ----------------------------------------------------

EMA = {"length": 5, "source": "close"}


def fx_walk(n, seed=11):
    rng = random.Random(seed)
    x, out = 1.1, []
    for _ in range(n):
        x += rng.choice([-1.0, -0.4, 0.3, 1.0]) * 1e-3
        out.append(round(x, 5))
    return out


ARM_AT = 99
COUNT = 180


async def run_mode(tmp_path, mode, thresholds, record=None, tag=""):
    db = await Database.open(tmp_path / f"{mode}{tag}.db")
    try:
        w = World(tf="15m", count=COUNT, closes=fx_walk(COUNT))  # inside the trading week
        real = w.compute

        async def compute(key, schema, settings, frame):
            out = await real(key, schema, settings, frame)
            if record is not None:
                record[int(frame["ts"].iloc[-1])] = float(out["ema"].iloc[-1])
            return out

        w.compute = compute
        h = Harness(AlertStore(db.conn), w)
        h.ev.compute = compute
        alerts = [
            await h.add(value=t, kind="builtin", ind="ema", output="ema", params=EMA)
            for t in thresholds
        ]
        await h.close(ARM_AT)
        if mode == "live":
            for i in range(ARM_AT + 1, COUNT):
                await h.close(i)
        else:
            await h.close(COUNT - 1)
        index = {a.id: k for k, a in enumerate(alerts)}
        log = sorted(
            (index[e.alert_id], e.bar, e.cross, e.value, e.observed, e.observed_prev, e.correction)
            for e in await h.store.events(10_000)
        )
        return log, [f.late for f in h.fires], w
    finally:
        await db.close()


@pytest.mark.parametrize("anchor_bars", [ie.ANCHOR_BARS, 16])
async def test_a_value_within_1e9_of_the_threshold_decides_the_same_live_and_in_replay(
    tmp_path, monkeypatch, anchor_bars
):
    """EMA has unbounded memory: a computation started on another bar gives values that
    differ in the last digits. The threshold of each alert is within 1e-9 of the value the
    live evaluation saw on a bar where it crossed; the replay after an outage must take
    the same decisions on the same evaluated values. With 16 bars per anchor cell, the
    replay spans several cells (one computation each)."""
    monkeypatch.setattr(ie, "ANCHOR_BARS", anchor_bars)
    live_values: dict[int, float] = {}
    await run_mode(tmp_path, "live", [1.1], record=live_values, tag="-probe")
    bars = sorted(live_values)
    thresholds = []
    for prev, bar in itertools.pairwise(bars):
        a, b = live_values[prev], live_values[bar]
        if abs(b - a) > 1e-6:  # a clear move: the threshold right next to the new value
            for d in (
                -9e-10,
                -1e-10,
                -1e-11,
                -1e-12,
                -1e-13,
                -1e-14,
                1e-14,
                1e-13,
                1e-12,
                1e-11,
                1e-10,
                9e-10,
            ):
                thresholds.append(b + d)
        if len(thresholds) >= 96:
            break
    assert len(thresholds) >= 32
    assert all(min(abs(t - v) for v in live_values.values()) <= 1e-9 for t in thresholds)

    live, live_late, _ = await run_mode(tmp_path, "live", thresholds)
    replay, replay_late, _ = await run_mode(tmp_path, "replay", thresholds)
    assert live, "no alert fired: the test would prove nothing"
    assert set(live_late) == {None} and set(replay_late) <= {"reconnect", None}
    assert replay == live  # the same fires, the same values, bit for bit
    assert all(x[-1] is None for x in live)  # no "correction" from computation noise (D5)


async def test_each_fire_logs_the_value_it_was_decided_on(store):  # noqa: F811
    w = World(closes=[-1, -1, 2, -3])
    h = Harness(store, w)
    alert = await h.add(value=0.5)
    for i in range(4):
        await h.close(i)
    events = sorted(await store.events(10), key=lambda e: e.bar)
    assert [(e.cross, e.value, e.observed, e.observed_prev) for e in events] == [
        ("up", 2.0, 1.5, -1.5),
        ("down", -3.0, -3.5, 1.5),
    ]
    assert all(isinstance(e.frame_start, int) for e in events)
    as_json = events[0].to_json()
    assert as_json["observed"] == 1.5 and as_json["observed_prev"] == -1.5
    assert as_json["frame_start"] == events[0].frame_start
    assert alert.fire_count == 2


def test_line_events_have_no_evaluation_fields():
    from openchart.store.alerts import AlertEvent

    e = AlertEvent(id=1, alert_id="x", ts=1, price=1.0, line_value=1.0, cross="up", message="")
    assert not {"observed", "observed_prev", "frame_start"} & set(e.to_json())


def test_ema_differs_between_two_frame_starts():
    """Why the test above is not trivial: the same EMA bar from two frames that start on
    different bars differs (by less than the declared warmup's 1e-8)."""
    ema = load_indicators().get("ema")
    closes = fx_walk(COUNT)
    frame = pd.DataFrame({"ts": range(COUNT), "o": closes, "h": closes, "l": closes, "c": closes})
    frame["v"] = 1.0
    settings = ema.normalize(EMA)
    warm = ema.warmup(settings)
    full = ema.run(frame, settings)["ema"].to_numpy()
    late = ema.run(frame.iloc[COUNT - warm - 2 :].reset_index(drop=True), settings)["ema"]
    d = abs(full[-1] - late.to_numpy()[-1])
    assert 0 < d < 1e-8


async def watch(c):
    """The watchdog's regular check, on the server's loop."""
    c.server.alerts.watch()


async def test_a_new_alert_in_a_group_replaying_several_cells_only_arms(store, monkeypatch):  # noqa: F811
    """A catch-up over several anchor cells is one computation per cell; an alert that
    joined the group meanwhile records the newest bar and fires on none of them."""
    monkeypatch.setattr(ie, "ANCHOR_BARS", 4)
    w = World(tf="15m", count=40, closes=[-1, 1] * 20)
    h = Harness(store, w)
    old = await h.add()
    await h.close(0)
    new = await h.add()  # the same group, added during an outage
    await h.close(30)
    assert w.computes > 2  # several cells
    assert {f.alert.id for f in h.fires} == {old.id} and len(h.fires) == 30
    assert new.istate.armed and new.istate.last_bar == w.starts[30] and new.fire_count == 0


async def test_a_fired_bar_moved_only_by_computation_noise_is_no_correction(store):  # noqa: F811
    """D5 records a correction when the broker's candles changed a fired bar, not when two
    computations of the same bars differ in the last digits."""
    w = World(closes=[-1, -1, 0.5 + 1e-12, 1, 1])
    h = Harness(store, w)
    await h.add(value=0.5)
    for i in range(3):
        await h.close(i)
    assert [f.cross for f in h.fires] == ["up"]
    bar = w.starts[2]

    def nudge(to):
        def answer(frame, out):
            c = out["out"].to_numpy().copy()
            c[frame["ts"].to_numpy() == bar] = to
            return {**out, "out": pd.Series(c)}

        return answer

    w.answer = nudge(0.5 - 1e-12)  # across the value, by far less than a candle moves
    await h.close(3)
    assert [e.correction for e in await store.events(10)] == [None]
    w.answer = nudge(0.4)  # the broker's candle really changed it
    await h.close(4)
    (event,) = await store.events(10)
    assert event.correction is not None and event.correction["value"] == 0.4
