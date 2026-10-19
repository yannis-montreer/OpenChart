"""Indicators per chart, on the server (CI-3, decision D1): the chart's instances, the
"settings reset" note, the one-time import of a browser's old list, saved layouts that
carry and restore a copy, the catalog's sections, and where a custom definition is used
and deleting it while it is (docs/custom-indicators-design.md sections 1.2, 4 and 5.4).

With the fake worker (tests/fake_worker.py): what is tested is what the server stores,
validates and sends, not how a script runs.
"""

import asyncio
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators.worker.isolation import Isolation
from openchart.market.instruments import FX_HOURS
from openchart.settings import INDICATOR_EDITING
from openchart.store.layouts import LayoutStore
from tests.conftest import EURO, Harness, run_setup
from tests.test_custom_indicators import wait_for_worker, write
from tests.worker_helpers import FakeLauncher
from tests.ws import receive_until

ISOLATED = Isolation("appcontainer", True, "minimum isolation met")
EMA_DEFAULTS = {"length": 9, "source": "close", "color": "#2962ff", "width": 1}
FAKE_DEFAULTS = {"color": "#2962ff", "width": 1}
LATER = 10**9  # ns added to a rewritten file's time, so coarse clocks see the change


@pytest.fixture
def cc(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder = harness.server, folder
        assert run_setup(c).status_code == 200
        c.portal.call(c.server.store.upsert_instrument, c.server.source, EURO, FX_HOURS)
        wait_for_worker(c)
        yield c


def url(c, instrument="XAU_USD", source=None):
    return f"/api/charts/{source or c.server.source}/{instrument}/indicators"


def chart(c, instrument="XAU_USD"):
    r = c.get(url(c, instrument))
    assert r.status_code == 200, r.text
    return r.json()


def put(c, indicators, instrument="XAU_USD", **extra):
    return c.put(url(c, instrument), json={"indicators": indicators, **extra})


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def allow_editing(c):
    """What CI-1's gate needs: the minimum isolation and the setting (the test client's
    peer is not loopback, so `auto` would refuse)."""
    c.server.custom.isolation = ISOLATED
    c.portal.call(c.server.settings.set, INDICATOR_EDITING, "on")


def entries(body):
    """The instances as a browser sends them back."""
    keys = ("uid", "kind", "id", "params", "visible", "price_labels", "legend_values")
    return [{k: i[k] for k in keys} for i in body["indicators"]]


# The chart's instances ----------------------------------------------------------------


def test_a_chart_starts_with_no_indicators(cc):
    assert chart(cc) == {
        "source": cc.server.source,
        "instrument": "XAU_USD",
        "revision": 0,
        "indicators": [],
    }


def test_put_stores_complete_settings_in_order_and_every_browser_hears_it(cc):
    with cc.websocket_connect("/ws") as ws:
        body = ok(
            put(
                cc,
                [
                    {"id": "ema", "params": {"length": 21}},
                    {"kind": "builtin", "id": "ema", "visible": False, "uid": "b2"},
                ],
            )
        )
        message = receive_until(ws, "chart_indicators_changed")
    assert message == {"type": "chart_indicators_changed", **body}
    assert body["revision"] == 1
    first, second = body["indicators"]
    assert re.fullmatch(r"[0-9a-f]{16}", first["uid"])
    assert first == {
        "uid": first["uid"],
        "kind": "builtin",
        "id": "ema",
        "params": {**EMA_DEFAULTS, "length": 21},
        "visible": True,
        "price_labels": True,
        "legend_values": True,
        "status": "ok",
        "reset": [],
    }
    assert second["uid"] == "b2" and second["visible"] is False
    assert second["params"] == EMA_DEFAULTS
    assert chart(cc) == body

    again = ok(put(cc, list(reversed(entries(body)))))
    assert [i["uid"] for i in again["indicators"]] == ["b2", first["uid"]]
    assert again["revision"] == 2
    assert chart(cc)["indicators"] == again["indicators"]


@pytest.mark.parametrize(
    "indicators, status",
    [
        ([{"id": "nope"}], 400),
        ([{"id": "ema", "params": {"length": "x"}}], 400),
        ([{"id": "ema", "params": {"length": 0}}], 400),
        ([{"id": "ema", "uid": "a"}, {"id": "ema", "uid": "a"}], 400),
        ([{"id": "ema", "kind": "custom"}], 400),  # no file of yours named ema
        ([{"id": "ema", "uid": "bad uid!"}], 422),
        ([{"id": "ema", "kind": "other"}], 422),
        ([{"id": "ema", "params": {"length": {"x": 1}}}], 422),
        ([{"id": "ema", "params": {"length": [1]}}], 422),
        ([{"id": "ema", "params": {"label": "x" * 1001}}], 422),
        ([{"id": "ema", "params": {f"p{i}": 1 for i in range(65)}}], 422),
        ([{"id": "ema", "params": {"length": 10**20}}], 400),  # a number; too large for it
        ([{"id": "ema", "params": {"length": 2**53 + 1}}], 400),
        ([{"id": "ema", "params": {f"p{i}": "x" * 1000 for i in range(9)}}], 422),  # > 8 KB
        ([{"id": "x" * 41}], 422),
        ([{"id": "ema"}] * 31, 422),
    ],
)
def test_bad_lists_are_refused_and_nothing_changes(cc, indicators, status):
    r = put(cc, indicators)
    assert r.status_code == status, r.text
    assert chart(cc)["revision"] == 0 and chart(cc)["indicators"] == []


@pytest.mark.parametrize("number", ["Infinity", "-Infinity", "NaN"])
def test_numbers_that_are_not_finite_are_refused(cc, number):
    text = '{"indicators": [{"id": "ema", "params": {"length": NUMBER}}]}'.replace("NUMBER", number)
    r = cc.put(url(cc), content=text, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert chart(cc)["revision"] == 0


def test_an_unknown_chart_is_404(cc):
    for path in (url(cc, "NOPE_USD"), url(cc, source="other")):
        assert cc.get(path).status_code == 404
        assert cc.put(path, json={"indicators": []}).status_code == 404


def test_charts_are_separate(cc):
    ok(put(cc, [{"id": "ema"}]))
    assert chart(cc, "EUR_USD") == {
        "source": cc.server.source,
        "instrument": "EUR_USD",
        "revision": 0,
        "indicators": [],
    }
    assert len(chart(cc)["indicators"]) == 1


def test_an_unknown_input_is_dropped_with_a_settings_reset_note(cc):
    body = ok(put(cc, [{"id": "ema", "params": {"length": 21, "old": 3}}]))
    (got,) = body["indicators"]
    assert got["params"] == {**EMA_DEFAULTS, "length": 21} and got["reset"] == ["old"]
    assert chart(cc)["indicators"][0]["reset"] == []  # stored as it now fits: noted once


def test_a_stale_revision_is_refused_with_the_current_list(cc):
    first = ok(put(cc, [{"id": "ema"}]))
    r = put(cc, [], revision=0)
    assert r.status_code == 409
    assert r.json()["detail"]["chart"] == first
    assert chart(cc) == first
    assert ok(put(cc, [], revision=1))["revision"] == 2


def test_builtin_and_custom_ids_are_separate_namespaces(cc):
    write(cc.folder, "ema.py", "PLAIN\n")
    write(cc.folder, "mine.py", "PLAIN\n")
    body = ok(
        put(
            cc,
            [
                {"kind": "builtin", "id": "ema"},
                {"kind": "custom", "id": "ema", "params": {"width": 3}},
                {"id": "mine"},  # only a file of yours has this id: custom
                {"id": "ema"},  # both have it: the built-in, as for old entries
            ],
        )
    )
    got = [(i["kind"], i["id"], i["params"]) for i in body["indicators"]]
    assert got == [
        ("builtin", "ema", EMA_DEFAULTS),
        ("custom", "ema", {**FAKE_DEFAULTS, "width": 3}),
        ("custom", "mine", FAKE_DEFAULTS),
        ("builtin", "ema", EMA_DEFAULTS),
    ]
    assert put(cc, [{"kind": "builtin", "id": "mine"}]).status_code == 400


def test_settings_that_no_longer_fit_an_edited_definition_fall_back_with_a_note(cc):
    write(cc.folder, "mode.py", "MODE\n")
    ok(put(cc, [{"id": "mode", "params": {"mode": "fast", "color": "#00ff00"}}]))
    write(cc.folder, "mode.py", "MODE2\n", later_ns=LATER)  # the choice "fast" renamed
    (got,) = chart(cc)["indicators"]
    assert got["params"] == {"color": "#00ff00", "mode": "quick"}
    assert got["reset"] == ["mode"] and got["status"] == "ok"
    # Reading does not change what is stored: the old file brings the setting back.
    write(cc.folder, "mode.py", "MODE again\n", later_ns=2 * LATER)
    (got,) = chart(cc)["indicators"]
    assert got["params"] == {"color": "#00ff00", "mode": "fast"} and got["reset"] == []

    write(cc.folder, "plain.py", "PLAIN\n")
    ok(put(cc, [{"id": "plain", "params": {"width": 3}}], "EUR_USD"))
    write(cc.folder, "plain.py", "MODE\n", later_ns=LATER)  # the width input removed
    (got,) = chart(cc, "EUR_USD")["indicators"]
    assert got["params"] == {"color": "#2962ff", "mode": "fast"} and got["reset"] == ["width"]


def test_an_instance_of_a_file_that_does_not_load_is_kept(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine", "params": {"width": 3}}]))
    write(cc.folder, "mine.py", "NODESCRIBE\n", later_ns=LATER)
    body = chart(cc)
    (got,) = body["indicators"]
    assert got["status"] == "error" and got["params"] == {**FAKE_DEFAULTS, "width": 3}
    # A browser sending the list back (another change on the chart) keeps it as it is.
    back = ok(put(cc, [*entries(body), {"id": "ema"}]))
    assert back["indicators"][0]["params"] == {**FAKE_DEFAULTS, "width": 3}
    # A new instance of it is stored as given, checked once the file loads again.
    added = ok(put(cc, [{"kind": "custom", "id": "mine", "params": {"width": 9}}]))
    assert added["indicators"][0]["params"] == {"width": 9}
    write(cc.folder, "mine.py", "PLAIN again\n", later_ns=2 * LATER)
    (got,) = chart(cc)["indicators"]
    assert got["status"] == "ok" and got["params"] == FAKE_DEFAULTS and got["reset"] == ["width"]

    (cc.folder / "mine.py").unlink()  # deleted outside the browser
    (got,) = chart(cc)["indicators"]
    assert got["status"] == "missing" and got["params"] == {"width": 9}
    assert put(cc, [{"kind": "custom", "id": "mine"}]).status_code == 400


# The one-time import of a browser's old list ---------------------------------------------


def test_a_browsers_old_list_is_imported_once(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    old = [
        {
            "id": "ema",
            "params": {"length": 21},
            "visible": False,
            "price_labels": True,
            "legend_values": False,
        },
        {"id": "mine", "params": {"width": 9}},  # out of range now: falls back, noted
        {"id": "vanished", "params": {}},
    ]
    with cc.websocket_connect("/ws") as ws:
        body = ok(cc.post(url(cc) + "/import", json={"indicators": old}))
        message = receive_until(ws, "chart_indicators_changed")
    assert message["revision"] == 1 and message["indicators"] == body["indicators"]
    ema, mine = body["indicators"]
    assert (ema["kind"], ema["params"], ema["visible"], ema["legend_values"]) == (
        "builtin",
        {**EMA_DEFAULTS, "length": 21},
        False,
        False,
    )
    assert (mine["kind"], mine["params"], mine["reset"]) == ("custom", FAKE_DEFAULTS, ["width"])
    assert [s["id"] for s in body["skipped"]] == ["vanished"]
    assert body["revision"] == 1

    again = cc.post(url(cc) + "/import", json={"indicators": old})
    assert again.status_code == 409
    assert len(chart(cc)["indicators"]) == 2 and chart(cc)["revision"] == 1


def test_the_import_is_refused_once_a_chart_was_set_even_to_nothing(cc):
    ok(put(cc, [], "EUR_USD"))
    r = cc.post(url(cc, "EUR_USD") + "/import", json={"indicators": [{"id": "ema"}]})
    assert r.status_code == 409
    assert chart(cc, "EUR_USD")["indicators"] == []


def test_the_import_takes_what_it_can_of_an_old_list(cc):
    """Review finding 4: the browser's old list had no limit and was never checked, so
    one entry outside PUT's bounds must not keep the whole list out for good."""
    old = [{"id": "ema", "params": {"length": n}} for n in range(2, 34)]  # 32 entries
    old[1] = {"id": "ema", "params": {"a": [1]}}  # not a value
    old[2] = {"id": "Not An Id"}
    old[3] = {"id": "ema", "params": {"length": "x" * 2000}}
    r = cc.post(url(cc) + "/import", json={"indicators": old})
    body = ok(r)
    assert len(body["indicators"]) == 29  # 32 less the 3 that are not indicators
    assert [i["params"]["length"] for i in body["indicators"]][:2] == [2, 6]
    reasons = [s["reason"] for s in body["skipped"]]
    assert len(reasons) == 3 and all("not an indicator" in x for x in reasons)
    nan = '{"indicators": [{"id": "ema", "params": {"length": NaN}}, {"id": "ema"}]}'
    r = cc.post(
        url(cc, "EUR_USD") + "/import", content=nan, headers={"content-type": "application/json"}
    )
    body = ok(r)
    assert len(body["indicators"]) == 1 and len(body["skipped"]) == 1


def test_the_import_keeps_the_first_30(cc):
    old = [{"id": "ema", "params": {"length": n}} for n in range(2, 34)]  # 32 entries
    body = ok(cc.post(url(cc) + "/import", json={"indicators": old}))
    assert [i["params"]["length"] for i in body["indicators"]] == list(range(2, 32))
    assert [s["reason"] for s in body["skipped"]] == ["more than 30 indicators"] * 2


def test_the_import_refuses_a_body_that_is_not_a_list(cc):
    for body in ({"indicators": {"id": "ema"}}, {"indicators": [{"id": "ema"}] * 201}, {}):
        assert cc.post(url(cc) + "/import", json=body).status_code == 422
    assert chart(cc)["revision"] == 0


# Saved layouts carry a copy and restore it -----------------------------------------------


def test_a_layout_saved_without_indicators_copies_the_charts(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    body = ok(put(cc, [{"id": "ema", "params": {"length": 21}}, {"id": "mine"}]))
    saved = ok(cc.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    assert saved["layout"]["indicators"] == entries(body)

    # An M7 browser sends its own list: kept as before, with a kind and a uid added.
    m7 = {"instrument": "XAU_USD", "indicators": [{"id": "ema"}, {"id": "mine"}]}
    saved = ok(cc.post("/api/layouts", json={"name": "M7", "layout": m7}))
    got = saved["layout"]["indicators"]
    assert [(i["kind"], i["id"]) for i in got] == [("builtin", "ema"), ("custom", "mine")]
    assert all(re.fullmatch(r"[0-9a-f]{16}", i["uid"]) for i in got)
    assert got[1]["params"] == FAKE_DEFAULTS

    bad = {"instrument": "XAU_USD", "indicators": [{"id": "ema", "kind": "custom"}]}
    assert cc.post("/api/layouts", json={"name": "Bad", "layout": bad}).status_code == 400


def test_restoring_a_layout_puts_its_copy_on_the_chart(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    body = ok(put(cc, [{"id": "ema", "params": {"length": 21}}, {"id": "mine"}]))
    saved = ok(cc.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    ok(put(cc, []))
    with cc.websocket_connect("/ws") as ws:
        applied = ok(cc.post(f"/api/layouts/{saved['id']}/apply"))
        message = receive_until(ws, "chart_indicators_changed")
    assert applied["chart"]["revision"] == 3 and applied["skipped"] == []
    assert entries(applied["chart"]) == entries(body)  # the same uids too
    assert message == {"type": "chart_indicators_changed", **applied["chart"]}
    assert chart(cc) == applied["chart"]

    (cc.folder / "mine.py").unlink()
    applied = ok(cc.post(f"/api/layouts/{saved['id']}/apply"))
    assert [i["id"] for i in applied["chart"]["indicators"]] == ["ema"]
    assert [s["id"] for s in applied["skipped"]] == ["mine"]

    anywhere = {"indicators": [{"id": "ema"}]}
    other = ok(cc.post("/api/layouts", json={"name": "Any", "layout": anywhere}))
    assert cc.post(f"/api/layouts/{other['id']}/apply").status_code == 400  # which chart?
    applied = ok(cc.post(f"/api/layouts/{other['id']}/apply?instrument=EUR_USD"))
    assert applied["chart"]["instrument"] == "EUR_USD"
    assert [i["id"] for i in chart(cc, "EUR_USD")["indicators"]] == ["ema"]
    assert cc.post("/api/layouts/nope/apply?instrument=EUR_USD").status_code == 404
    assert cc.post(f"/api/layouts/{other['id']}/apply?instrument=NOPE").status_code == 404


def test_a_restored_layout_notes_settings_that_no_longer_fit(cc):
    write(cc.folder, "mode.py", "MODE\n")
    ok(put(cc, [{"id": "mode", "params": {"mode": "fast"}}]))
    saved = ok(cc.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    write(cc.folder, "mode.py", "MODE2\n", later_ns=LATER)
    applied = ok(cc.post(f"/api/layouts/{saved['id']}/apply"))
    (got,) = applied["chart"]["indicators"]
    assert got["params"]["mode"] == "quick" and got["reset"] == ["mode"]


# The catalog's sections -----------------------------------------------------------------


def test_the_catalog_has_builtin_and_custom_sections_with_kinds(cc):
    write(cc.folder, "ema.py", "PLAIN\n")
    write(cc.folder, "mine.py", "PLAIN\n")
    write(cc.folder, "broken.py", "NODESCRIBE\n")
    body = cc.get("/api/indicators").json()
    assert body["builtin"] and all(e["kind"] == "builtin" for e in body["builtin"])
    assert "ema" in {e["id"] for e in body["builtin"]}
    custom = {e["id"]: e for e in body["custom"]}
    assert set(custom) == {"broken", "ema", "mine"}
    assert all(e["kind"] == "custom" for e in custom.values())
    assert custom["mine"]["definition"]["kind"] == "custom"
    assert custom["broken"]["definition"] is None and custom["broken"]["status"] == "error"
    assert body["errors"] and all(e["kind"] in ("builtin", "custom") for e in body["errors"])
    assert {(e["kind"], e["file"]) for e in body["errors"]} == {("custom", "broken.py")}
    flat = [(e["kind"], e["id"]) for e in body["indicators"]]
    builtin = {"ema", "sma", "rsi", "macd", "bollinger", "atr", "stochastic", "volume"}
    assert sorted(flat) == sorted([("builtin", i) for i in builtin] + [("custom", "mine")])


# Where a custom definition is used, and deleting it -------------------------------------


def in_use_everywhere(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    history = cc.folder / ".history" / "mine"
    history.mkdir(parents=True)
    (history / "20261014T101502Z.py").write_text("PLAIN old\n")
    ok(put(cc, [{"id": "mine"}, {"id": "ema"}, {"id": "mine"}]))
    ok(put(cc, [{"id": "mine"}], "EUR_USD"))
    gold = ok(cc.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    other = {"instrument": "XAU_USD", "indicators": [{"id": "ema"}]}
    ok(cc.post("/api/layouts", json={"name": "Other", "layout": other}))
    return gold


def test_usage_lists_the_charts_and_layouts(cc):
    allow_editing(cc)
    gold = in_use_everywhere(cc)
    usage = ok(cc.get("/api/custom-indicators/mine/usage"))
    source = cc.server.source
    assert usage == {
        "id": "mine",
        "in_use": True,
        "charts": [
            {"source": source, "instrument": "EUR_USD", "instances": 1},
            {"source": source, "instrument": "XAU_USD", "instances": 2},
        ],
        "layouts": [{"id": gold["id"], "name": "Gold", "instances": 2}],
    }
    assert cc.get("/api/custom-indicators/nope/usage").status_code == 404


def test_deleting_a_definition_in_use_needs_confirmation(cc):
    allow_editing(cc)
    gold = in_use_everywhere(cc)
    refused = cc.delete("/api/custom-indicators/mine")
    assert refused.status_code == 409
    usage = ok(cc.get("/api/custom-indicators/mine/usage"))
    assert refused.json()["detail"]["usage"] == usage
    assert "2 charts" in refused.json()["detail"]["message"]
    assert (cc.folder / "mine.py").is_file() and len(chart(cc)["indicators"]) == 3

    with cc.websocket_connect("/ws") as ws:
        body = ok(cc.delete("/api/custom-indicators/mine?confirm=true"))
        changed = [receive_until(ws, "chart_indicators_changed") for _ in range(2)]
        receive_until(ws, "layouts_changed")
        receive_until(ws, "indicators_changed")
    assert {m["instrument"] for m in changed} == {"EUR_USD", "XAU_USD"}
    assert body["removed"] == usage
    assert re.fullmatch(r"\.deleted/mine-\d{8}T\d{6}Z", body["moved_to"])
    moved = cc.folder / body["moved_to"]
    assert (moved / "mine.py").read_text() == "PLAIN\n"
    assert (moved / "history" / "20261014T101502Z.py").read_text() == "PLAIN old\n"
    assert not (cc.folder / "mine.py").exists() and not (cc.folder / ".history" / "mine").exists()

    assert [i["id"] for i in chart(cc)["indicators"]] == ["ema"]
    assert chart(cc, "EUR_USD")["indicators"] == []
    layout = ok(cc.get(f"/api/layouts/{gold['id']}"))["layout"]
    assert [i["id"] for i in layout["indicators"]] == ["ema"]
    assert "mine" not in {e["id"] for e in cc.get("/api/indicators").json()["custom"]}
    assert cc.delete("/api/custom-indicators/mine?confirm=true").status_code == 404


def test_a_definition_not_in_use_is_deleted_without_confirmation(cc):
    allow_editing(cc)
    write(cc.folder, "idle.py", "PLAIN\n")
    body = ok(cc.delete("/api/custom-indicators/idle"))
    assert body["removed"]["in_use"] is False
    assert (cc.folder / body["moved_to"] / "idle.py").is_file()
    assert not (cc.folder / "idle.py").exists()


def test_deleting_a_custom_ema_leaves_the_builtin_ema(cc):
    allow_editing(cc)
    write(cc.folder, "ema.py", "PLAIN\n")
    write(cc.folder, "solo.py", "PLAIN\n")
    ok(put(cc, [{"kind": "builtin", "id": "ema"}, {"kind": "custom", "id": "ema"}]))
    # Layouts saved before CI-3 have entries without a kind: "ema" is the built-in one,
    # "solo" (only a file of yours has that id) the custom one.
    old = {"instrument": "XAU_USD", "indicators": [{"id": "ema", "params": {}}, {"id": "solo"}]}
    layout = cc.portal.call(LayoutStore(cc.server.db.conn).create, "Old", old)
    usage = ok(cc.get("/api/custom-indicators/ema/usage"))
    assert usage["layouts"] == [] and usage["charts"][0]["instances"] == 1
    assert ok(cc.get("/api/custom-indicators/solo/usage"))["layouts"][0]["instances"] == 1

    ok(cc.delete("/api/custom-indicators/ema?confirm=true"))
    assert [(i["kind"], i["id"]) for i in chart(cc)["indicators"]] == [("builtin", "ema")]
    assert [i["id"] for i in ok(cc.get(f"/api/layouts/{layout.id}"))["layout"]["indicators"]] == [
        "ema",
        "solo",
    ]
    ok(cc.delete("/api/custom-indicators/solo?confirm=true"))
    assert [i["id"] for i in ok(cc.get(f"/api/layouts/{layout.id}"))["layout"]["indicators"]] == [
        "ema"
    ]
    assert "ema" in {e["id"] for e in cc.get("/api/indicators").json()["builtin"]}


def test_usage_and_delete_follow_the_editing_gate(cc):
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine"}]))
    paths = ["/api/custom-indicators/mine/usage"]
    # The fake worker is a plain process: below the minimum isolation, "on" does not help.
    cc.portal.call(cc.server.settings.set, INDICATOR_EDITING, "on")
    assert cc.get(paths[0]).status_code == 403
    assert cc.delete("/api/custom-indicators/mine?confirm=true").status_code == 403
    cc.server.custom.isolation = ISOLATED
    cc.portal.call(cc.server.settings.set, INDICATOR_EDITING, "off")
    r = cc.delete("/api/custom-indicators/mine?confirm=true")
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    cc.portal.call(cc.server.settings.set, INDICATOR_EDITING, "auto")  # testclient is no loopback
    assert cc.delete("/api/custom-indicators/mine?confirm=true").status_code == 403
    assert (cc.folder / "mine.py").is_file() and len(chart(cc)["indicators"]) == 1
    assert not (cc.folder / ".deleted").exists()


@pytest.mark.parametrize("name", ["..%2Fmine", "Mine", "mine%0A", ".deleted", "_mine", "x" * 41])
def test_delete_refuses_names_that_are_not_ids(cc, name):
    allow_editing(cc)
    write(cc.folder, "mine.py", "PLAIN\n")
    write(cc.folder, "_mine.py", "PLAIN\n")
    assert cc.delete(f"/api/custom-indicators/{name}?confirm=true").status_code == 404
    assert cc.get(f"/api/custom-indicators/{name}/usage").status_code == 404
    assert (cc.folder / "mine.py").is_file() and (cc.folder / "_mine.py").is_file()


def test_delete_refuses_a_deleted_folder_that_is_not_a_folder(cc):
    allow_editing(cc)
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine"}]))
    (cc.folder / ".deleted").write_text("not a folder")
    r = cc.delete("/api/custom-indicators/mine?confirm=true")
    assert r.status_code == 409 and ".deleted" in r.json()["detail"]
    assert (cc.folder / "mine.py").is_file()
    assert len(chart(cc)["indicators"]) == 1  # nothing removed before the move could happen


def test_the_chart_routes_need_no_editing(cc):
    """Adding an indicator from the phone is not editing code."""
    cc.portal.call(cc.server.settings.set, INDICATOR_EDITING, "off")
    write(cc.folder, "mine.py", "PLAIN\n")
    assert put(cc, [{"id": "mine"}, {"id": "ema"}]).status_code == 200
    assert json.dumps(chart(cc))  # and reading it


def test_a_chart_of_another_source_is_404_even_with_its_instruments_stored(cc):
    """Instruments are stored per source: a database that once had another broker keeps
    its rows, but this server's charts are only its own source's."""
    cc.portal.call(cc.server.store.upsert_instrument, "other", EURO, FX_HOURS)
    assert cc.get(url(cc, "EUR_USD", source="other")).status_code == 404
    assert put(cc, [], "EUR_USD").status_code == 200  # this server's own is fine


def test_apply_puts_a_layout_on_the_instrument_asked_for(cc):
    ok(put(cc, [{"id": "ema", "params": {"length": 21}}]))
    saved = ok(cc.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": "XAU_USD"}}))
    applied = ok(cc.post(f"/api/layouts/{saved['id']}/apply?instrument=EUR_USD"))
    assert applied["chart"]["instrument"] == "EUR_USD"
    assert chart(cc, "EUR_USD")["indicators"][0]["params"]["length"] == 21
    assert chart(cc)["revision"] == 1  # the layout's own chart is untouched


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold a newline")
def test_delete_refuses_a_file_whose_name_ends_in_a_newline(cc):
    allow_editing(cc)
    (cc.folder / "mine\n.py").write_text("PLAIN\n")
    assert cc.delete("/api/custom-indicators/mine%0A?confirm=true").status_code == 404
    assert (cc.folder / "mine\n.py").is_file()


# Found in the review of this change ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"indicators": [], "revision": True},  # would match revision 1
        {"indicators": [], "revision": "0"},
        {"indicators": [], "revision": -1},
        {"indicators": [{"id": "ema", "visible": "no"}]},
        {"indicators": [{"id": "ema", "price_labels": 0}]},
        {"indicators": [{"id": "ema", "legend_values": "false"}]},
    ],
)
def test_revision_and_switches_take_only_their_own_types(cc, body):
    """Review finding 3: lax parsing read `true` as revision 1 and "no" as false."""
    assert cc.put(url(cc), json=body).status_code == 422
    assert chart(cc)["revision"] == 0


def pause_delete_in_usage(monkeypatch):
    """The first read of the saved layouts (a delete's where-used, made inside the lock,
    before anything moves) answers, then waits 1 s with its answer; `inside` is set while
    it waits. A write that gets past the lock now is not in that answer."""
    import threading

    from openchart.store import layouts as store_module

    inside = threading.Event()
    real = store_module.LayoutStore.list
    first = []

    async def slow(self):
        got = await real(self)
        if not first:
            first.append(1)
            inside.set()
            await asyncio.sleep(1.0)
        return got

    monkeypatch.setattr(store_module.LayoutStore, "list", slow)
    return inside


def in_thread(fn):
    import threading

    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    thread.start()
    return thread, out


def test_a_layout_saved_during_a_delete_does_not_keep_the_deleted_definition(cc, monkeypatch):
    """Review finding 1: layout writes did not take the charts' lock, so a layout saved
    while a delete had counted where the definition is used kept one of the definition it
    deleted. (The delete moves the file before it removes instances, so the save must be
    held back from the count on, not only from the removal.)"""
    allow_editing(cc)
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine"}]))
    inside = pause_delete_in_usage(monkeypatch)
    thread, out = in_thread(lambda: cc.delete("/api/custom-indicators/mine?confirm=true"))
    assert inside.wait(10)
    layout = {"instrument": "EUR_USD", "indicators": [{"id": "mine"}, {"id": "ema"}]}
    saved = cc.post("/api/layouts", json={"name": "During", "layout": layout})
    thread.join(10)
    assert out["r"].status_code == 200
    assert not (cc.folder / "mine.py").exists()
    if saved.status_code == 200:  # saved first: then the delete must have taken it out
        got = ok(cc.get(f"/api/layouts/{saved.json()['id']}"))["layout"]["indicators"]
        assert [i["id"] for i in got] == ["ema"]
    else:
        assert saved.status_code == 400  # saved after: the definition is gone


def test_two_deletes_at_once_move_the_file_once(cc, monkeypatch):
    """Review finding 2: the file was looked up before the lock, so a second delete that
    waited for the first failed on the missing file (500) and left an empty folder. The
    second one gets past the first look while the file is still there (the first is held
    in its count, before it moves anything) and must look again under the lock."""
    allow_editing(cc)
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine"}]))
    inside = pause_delete_in_usage(monkeypatch)
    thread, out = in_thread(lambda: cc.delete("/api/custom-indicators/mine?confirm=true"))
    assert inside.wait(10)
    second = cc.delete("/api/custom-indicators/mine?confirm=true")
    thread.join(10)
    assert sorted([out["r"].status_code, second.status_code]) == [200, 404]
    assert len(list((cc.folder / ".deleted").iterdir())) == 1


def test_a_delete_whose_files_cannot_move_never_touches_the_charts(cc, monkeypatch):
    """The files move first and the instances only after: a move that fails (409) leaves
    the chart's revision where it was. Removing the instances first and putting them back
    would give the same list but a moved revision (and two change messages)."""
    allow_editing(cc)
    write(cc.folder, "mine.py", "PLAIN\n")
    ok(put(cc, [{"id": "mine"}, {"id": "ema"}]))
    before = chart(cc)["revision"]
    from openchart.api import routes_custom_indicators as module

    def refuse(src, dst):
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(module.os, "replace", refuse)
    r = cc.delete("/api/custom-indicators/mine?confirm=true")
    monkeypatch.undo()
    assert r.status_code == 409
    assert (cc.folder / "mine.py").is_file()
    after = chart(cc)
    assert after["revision"] == before
    assert [i["id"] for i in after["indicators"]] == ["mine", "ema"]


NAN_ENTRY = '{"id": "ema", "params": {"length": NaN}}'


@pytest.mark.parametrize(
    "path, entries",
    [("", [NAN_ENTRY] * 31), ("/import", ['{"length": NaN}'] * 201)],
    ids=["put-31", "import-201"],
)
def test_a_refused_body_holding_nan_is_a_422(cc, path, entries):
    """Review finding 5: FastAPI's 422 echoes the input, and a NaN in it could not be
    encoded, so a 31-entry list with a NaN was a 500."""
    method = cc.post if path else cc.put
    body = '{"indicators": [' + ", ".join(entries) + "]}"
    r = method(url(cc) + path, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert '"nan"' in r.text  # echoed as text


def test_the_same_422_on_other_routes(cc):
    """Finding 5's handler is the app's: a drawing with a NaN price was a 500 too."""
    body = '{"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1760450400, "p": NaN}]}'
    r = cc.post("/api/drawings", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422 and '"nan"' in r.text
