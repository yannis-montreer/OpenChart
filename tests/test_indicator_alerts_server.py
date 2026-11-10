"""Indicator alerts in the server, with the fake worker (`tests/fake_worker.py`): the
alerts' place in the worker's queue, a worker killed in the middle of an evaluation, a
script that runs past its deadline or is quarantined, the chart's writes stopping or
re-arming alerts, the message on a fire, and evaluation while the feed runs."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from openchart.alerts import indicator_eval as ie
from openchart.api.app import create_app
from openchart.providers.base import Tick
from tests.bounded import call
from tests.conftest import CANDLES, Harness, run_setup
from tests.test_check_scheduling import BARS, make, script, until
from tests.test_custom_indicators import wait_for_worker, write
from tests.worker_helpers import FakeLauncher

LAST = CANDLES[-1]


def put_chart(c, instances, instrument="XAU_USD"):
    url = f"/api/charts/{c.server.source}/{instrument}/indicators"
    r = c.put(url, json={"indicators": instances})
    assert r.status_code == 200, r.text
    return r.json()


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def alert_body(uid="m1", output="out", value=None, tf="1m"):
    value = LAST.mid.c if value is None else value
    return {
        "instrument": "XAU_USD",
        "type": "indicator",
        "timeframe": tf,
        "frequency": "repeating",
        "indicator": {"uid": uid, "output": output, "condition": "cross_value", "value": value},
    }


@pytest.fixture
def wk(tmp_path):
    """A set-up server with the fake worker and a file of yours on the gold chart."""
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    (gate / "go").write_text("")
    write(folder, "mine.py", f"GATE {gate}\n")
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder, c.gate = harness.server, folder, gate
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        put_chart(c, [{"uid": "m1", "kind": "custom", "id": "mine", "params": {}}])
        ind = c.server.alerts.indicators
        call(c, ind.stop)  # the tests drive the runs, on their own clock
        ind.evaluator.fresh = lambda _i, _t: True  # prices known (the unit tests cover it)
        c.sent = []
        real = c.server.alerts.dispatcher.system
        c.server.alerts.dispatcher.system = lambda text: (c.sent.append(text), real(text))
        yield c


def run(c, now):
    return call(c, c.server.alerts.indicators.run_once, now)


def armed_back(c, alert_id, minutes):
    """As if the alert had been evaluated `minutes` bars before the last stored minute."""
    alert = c.server.alerts.engine.alerts[alert_id]
    alert.istate.armed = True
    alert.istate.last_bar = LAST.ts - minutes * 60
    closes = {k.ts: k.mid.c for k in CANDLES}
    alert.istate.prev = ie.observation(closes[alert.istate.last_bar], alert.indicator.value)
    return alert


def expected_fires(alert, minutes):
    closes = [k.mid.c for k in CANDLES[-minutes - 1 :]]
    prev, out = None, []
    for i, c in enumerate(closes):
        s = ie.observation(c, alert.indicator.value)
        x = ie.crossing("cross_value", None, prev, s) if i else None
        prev = s
        if x:
            out.append((CANDLES[-minutes - 1 + i].ts, x))
    return out


def a_level_that_crosses(minutes):
    closes = [k.mid.c for k in CANDLES[-minutes - 1 :]]
    return round(sorted(closes)[len(closes) // 2] + 1e-4, 6)


# The worker's queue -------------------------------------------------------------------


async def test_alerts_wait_behind_charts_and_go_before_the_checks(tmp_path):
    _, runner, _ = await make(tmp_path)
    gate = tmp_path / "gate"
    gate.mkdir()
    held = script(f"GATE {gate}\n")
    try:
        (gate / "go").write_text("")
        await runner.compute(*held, {}, BARS)
        (gate / "go").unlink()
        finished = []

        async def job(name, priority):
            await runner.compute(*held, {}, BARS, priority=priority)
            finished.append(name)

        first = asyncio.create_task(job("chart0", "normal"))
        await until(lambda: len(list(gate.glob("started-*"))) == 2, "chart0 to start")
        jobs = [asyncio.create_task(job("check", "low"))]
        await until(lambda: runner._waiting["low"] == 1, "the check to queue")
        jobs.append(asyncio.create_task(job("alert", "alert")))
        await until(lambda: runner._waiting["alert"] == 1, "the alert to queue")
        jobs.append(asyncio.create_task(job("chart1", "normal")))
        await until(lambda: runner._waiting["normal"] == 1, "the chart to queue")
        (gate / "go").write_text("")
        await asyncio.gather(first, *jobs)
        assert finished == ["chart0", "chart1", "alert", "check"]
        with pytest.raises(ValueError):
            await runner.compute(*held, {}, BARS, priority="urgent")
    finally:
        (gate / "go").write_text("")
        await runner.stop()


# Failures of the worker --------------------------------------------------------------


def test_a_worker_killed_mid_evaluation_loses_and_doubles_nothing(wk):
    minutes = 30
    alert = ok(wk.post("/api/alerts", json=alert_body(value=a_level_that_crosses(minutes))))
    live = armed_back(wk, alert["id"], minutes)
    expected = expected_fires(live, minutes)
    assert expected
    now = LAST.ts + 60 + 5
    (wk.gate / "go").unlink()
    started = len(list(wk.gate.glob("started-*")))

    async def kill_when_started():
        await until(lambda: len(list(wk.gate.glob("started-*"))) > started, "the job to start")
        wk.server.indicator_runner.kill_for_tests()

    async def both():
        killer = asyncio.ensure_future(kill_when_started())
        notice = await wk.server.alerts.indicators.run_once(now)
        await killer
        return notice

    notice = call(wk, both)
    assert notice.fires == [] and live.istate.error
    assert live.istate.last_bar == LAST.ts - minutes * 60  # nothing evaluated
    assert len([m for m in wk.sent if "cannot be evaluated" in m]) == 1
    (wk.gate / "go").write_text("")
    for k in range(1, 4):  # retried until the new worker answers
        notice = run(wk, now + k * ie.RETRY_SECONDS)
        if notice.fires:
            break
    got = [(f.bar, f.cross) for f in notice.fires]
    assert got == expected
    assert {f.late for f in notice.fires} == {"worker"}
    assert len([m for m in wk.sent if "evaluated again" in m]) == 1
    assert run(wk, now + 100).fires == []  # nothing twice
    events = ok(wk.get(f"/api/alerts/events?alert_id={alert['id']}"))
    assert sorted(e["bar"] for e in events) == [b for b, _ in expected]
    assert all(e["late"] == "worker" and e["message"].startswith("🔔") for e in events)


def test_a_script_past_its_deadline_is_one_episode(wk):
    write(wk.folder, "slow.py", "HANG\n")
    put_chart(wk, [{"uid": "s1", "kind": "custom", "id": "slow", "params": {}}])
    # The HANG script describes itself fine; only its computations hang.
    ok(wk.post("/api/alerts", json=alert_body(uid="s1")))
    now = LAST.ts + 65
    t = time.monotonic()
    run(wk, now)
    assert time.monotonic() - t >= 4  # the worker's 5 s deadline
    (alert,) = [a for a in wk.server.alerts.engine.alerts.values() if a.type == "indicator"]
    assert "timed out" in alert.istate.error and not alert.istate.armed
    errors = [alert.istate.error]
    for k in range(1, 7):
        run(wk, now + k * ie.RETRY_SECONDS)
        errors.append(alert.istate.error)
        if call(wk, wk.server.custom.get, "slow").status == "quarantined":
            break
    assert errors[:3] == ["timed out after 5 s"] * 3  # each retry ran it again
    # Three timeouts: the file is quarantined; the alert says so, still one message.
    run(wk, now + 10 * ie.RETRY_SECONDS)
    assert call(wk, wk.server.custom.get, "slow").status == "quarantined"
    assert "quarantined" in alert.istate.error.lower()
    assert len([m for m in wk.sent if "cannot be evaluated" in m]) == 1


# The chart's writes ------------------------------------------------------------------


def test_removing_the_instance_stops_its_alerts_with_one_message(wk):
    first = ok(wk.post("/api/alerts", json=alert_body()))
    second = ok(wk.post("/api/alerts", json=alert_body(value=1.0)))
    run(wk, LAST.ts + 65)
    put_chart(wk, [])
    for a in (first, second):
        got = ok(wk.get("/api/alerts?uid=m1"))
        assert all(x["status"] == "stopped" for x in got)
        assert all(x["stop_reason"] == ie.STOP_REMOVED for x in got)
        assert {x["indicator"]["value"] for x in got} == {first["indicator"]["value"], 1.0}
        assert a["id"] in {x["id"] for x in got}
    assert len([m for m in wk.sent if "removed from the XAU_USD chart" in m]) == 1
    # Back on the chart, it can be started again.
    put_chart(wk, [{"uid": "m1", "kind": "custom", "id": "mine", "params": {}}])
    started = ok(wk.put(f"/api/alerts/{first['id']}", json={"status": "active"}))
    assert started["status"] == "active" and started["stop_reason"] is None


def test_new_values_on_the_chart_rearm(wk):
    put_chart(wk, [{"uid": "e1", "kind": "builtin", "id": "sma", "params": {"length": 5}}])
    alert = ok(wk.post("/api/alerts", json=alert_body(uid="e1", output="sma")))
    run(wk, LAST.ts + 65)
    live = wk.server.alerts.engine.alerts[alert["id"]]
    assert live.istate.armed
    put_chart(
        wk,
        [
            {
                "uid": "e1",
                "kind": "builtin",
                "id": "sma",
                "params": {"length": 5, "color": "#ff0000"},
            }
        ],
    )
    assert live.istate.armed  # a colour is not a value: nothing changes
    put_chart(wk, [{"uid": "e1", "kind": "builtin", "id": "sma", "params": {"length": 8}}])
    assert not live.istate.armed and live.indicator.params["length"] == 8
    assert ok(wk.get("/api/alerts"))[0]["state"]["armed"] is False


def test_an_edited_file_rearms(wk):
    alert = ok(wk.post("/api/alerts", json=alert_body()))
    run(wk, LAST.ts + 65)
    live = wk.server.alerts.engine.alerts[alert["id"]]
    assert live.istate.armed
    write(wk.folder, "mine.py", f"GATE {wk.gate}\n# edited\n", later_ns=10**9)
    run(wk, LAST.ts + 125)
    assert not live.istate.armed and live.status == "active"
    assert live.indicator.digest == call(wk, wk.server.custom.get, "mine").digest
    run(wk, LAST.ts + 185)
    assert live.istate.armed  # the first evaluation after the edit recorded


# The message ------------------------------------------------------------------------


def test_the_message_says_what_crossed_and_when(wk):
    minutes = 20
    alert = ok(wk.post("/api/alerts", json=alert_body(value=a_level_that_crosses(minutes))))
    live = armed_back(wk, alert["id"], minutes)
    live.notify = ["telegram", "app"]
    notice = run(wk, LAST.ts + 65)
    assert notice.fires
    text = notice.fires[0].event.message
    assert f"XAU_USD 1m Fake out crossed {notice.fires[0].cross}" in text
    assert "at the bar close" in text and "Late" in text  # replayed bars are late


# The feed running -------------------------------------------------------------------


def test_evaluation_while_the_feed_runs_and_alerts_are_edited(client):
    """Ticks pushed from another thread, evaluation runs and API edits at the same time:
    no error, one event per alert and bar, and the stored state is the live one."""
    put_chart(client, [{"uid": "e1", "kind": "builtin", "id": "sma", "params": {"length": 3}}])
    ids = [
        ok(client.post("/api/alerts", json=alert_body(uid="e1", output="sma", value=v)))["id"]
        for v in (LAST.mid.c, LAST.mid.c + 0.5, LAST.mid.c - 0.5)
    ]
    call(client, client.server.alerts.indicators.stop)
    ev = client.server.alerts.indicators.evaluator
    ev.fresh = lambda _i, _t: True
    stop = threading.Event()
    errors: list[BaseException] = []
    start = time.time()

    def feed():
        k = 0
        try:
            while not stop.is_set():
                k += 1
                p = LAST.mid.c + (1 if k % 2 else -1) * 0.7
                call(
                    client,
                    client.server.feed.handle_tick,
                    Tick("XAU_USD", start + k * 0.5, p - 0.1, p + 0.1),
                )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=feed)
    thread.start()
    try:
        for k in range(30):
            run(client, start + 60 * k)
            if k % 5 == 0:
                ok(client.put(f"/api/alerts/{ids[k % 3]}", json={"name": f"n{k}"}))
            if k % 7 == 0:
                ok(client.put(f"/api/alerts/{ids[1]}", json={"timeframe": "1m"}))
    finally:
        stop.set()
        thread.join(30)
    assert not errors
    events = ok(client.get("/api/alerts/events?limit=1000"))
    keys = [(e["alert_id"], e["bar"]) for e in events if e.get("bar") is not None]
    assert len(keys) == len(set(keys))
    for alert_id in ids:
        stored = call(client, client.server.alerts.store.get, alert_id)
        live = client.server.alerts.engine.alerts[alert_id]
        assert stored.istate.last_bar == live.istate.last_bar
        assert stored.fire_count == live.fire_count
