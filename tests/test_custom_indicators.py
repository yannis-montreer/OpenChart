"""Your own indicator files run in the worker, never in the server (CI-1).

Through the API with the real worker this machine's user gets (restricted on Windows, a
child process elsewhere): M7 files describe and compute as before, files are never
imported into the server, edits are picked up without a restart, custom and built-in ids
are separate, errors name their line. With the fake worker: the feed and alerts keep
running while a script hangs.
"""

import json
import os
import sys
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators._registry import BUILTIN_DIR, from_module
from openchart.indicators.worker.runner import Limits
from openchart.providers.base import Tick
from tests.conftest import Harness, run_setup
from tests.test_indicators import candles, reference_ema
from tests.worker_helpers import FakeLauncher, real_launchers
from tests.ws import receive_until

GOOD = """
from openchart.indicators import Color, Int, Output, Source, Width, sma, source

NAME = "Mid"
TITLE = "Middle line"
OVERLAY = False
PARAMS = [Int("length", 5, min=2), Source(), Color("color", "#ff0000"), Width("width", 2)]
OUTPUTS = [Output("mid", "Mid", color="color", width="width")]

def compute(bars, params):
    return {"mid": sma(source(bars, params["source"]), params["length"])}
"""


def write(folder, name, text, *, later_ns: int = 0):
    path = folder / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    if later_ns:  # file systems with coarse times: make the change visible
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + later_ns))
    return path


@pytest.fixture(scope="module")
def wc(tmp_path_factory):
    """A server with the real worker, shared by this module (a worker takes 1-3 s)."""
    harness = Harness(
        tmp_path_factory.mktemp("custom"),
        indicator_launchers=real_launchers(Limits(timeout_s=10)),
    )
    harness.seed_candles()
    (harness.config.data_dir / "indicators").mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.folder = harness.config.data_dir / "indicators"
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def wait_for_worker(c, seconds: float = 60) -> None:
    end = time.monotonic() + seconds
    while c.server.isolation is None:
        assert time.monotonic() < end, "the worker did not start"
        time.sleep(0.1)


def catalog(c):
    body = c.get("/api/indicators").json()
    return body, {d["id"]: d for d in body["custom"]}


def values(c, indicator, params=None, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": json.dumps(params or {}), **query}
    return c.get(f"/api/indicators/{indicator}", params=q)


def test_the_worker_starts_and_reports_its_isolation(wc):
    body, _ = catalog(wc)
    iso = body["isolation"]
    assert iso["level"] in ("restricted", "process-only")
    assert iso["minimum_met"] is False  # a source install: below the minimum (finding 5)
    assert iso["notice"].startswith("weaker isolation")
    assert iso["probes"]["environment"] == "clean"
    assert body["worker"]["state"] == "ready"
    assert body["editing"]["allowed"] is False  # below the minimum, whatever the setting


def test_an_m7_file_describes_and_computes_as_before(wc):
    write(wc.folder, "mid.py", GOOD)
    body, custom = catalog(wc)
    mid = custom["mid"]
    assert mid["status"] == "ok" and mid["file"] == "mid.py"
    namespace: dict = {}
    exec(compile(textwrap.dedent(GOOD), "mid.py", "exec"), namespace)  # the M7 loader's view
    in_server = from_module("mid", type("M", (), namespace), builtin=False).to_json()
    assert mid["definition"] == {**in_server, "kind": "custom"}
    # The M7 menu's flat list has it, after "Middle line" < "Moving Average Exponential".
    assert [(i["id"], i["builtin"]) for i in body["indicators"]] == [("mid", False), ("ema", True)]

    got = values(wc, "mid", params={"length": 3}, **{"from": 0, "limit": 10}).json()
    closes = [b["c"] for b in candles(wc, **{"from": 0, "limit": 10})]
    assert got["kind"] == "custom"
    assert got["values"]["mid"][:2] == [None, None]
    assert got["values"]["mid"][2] == pytest.approx(sum(closes[:3]) / 3)


def test_the_builtin_ema_file_runs_the_same_in_the_worker(wc):
    write(wc.folder, "my_ema.py", (BUILTIN_DIR / "ema.py").read_text(encoding="utf-8"))
    _, custom = catalog(wc)
    builtin = wc.get("/api/indicators").json()["builtin"][0]
    assert {
        k: v
        for k, v in custom["my_ema"]["definition"].items()
        if k not in ("id", "builtin", "kind")
    } == {k: v for k, v in builtin.items() if k not in ("id", "builtin", "kind")}
    q = {"from": 0, "limit": 300, "params": {"length": 9}}
    mine = values(wc, "my_ema", **q).json()["values"]["ema"]
    theirs = values(wc, "ema", **q).json()["values"]["ema"]
    assert mine == theirs
    closes = [b["c"] for b in candles(wc, **{"from": 0, "limit": 300})]
    assert mine == pytest.approx(reference_ema(closes, 9))


def test_files_are_never_imported_into_the_server(wc, tmp_path):
    marker = tmp_path / "imported.txt"
    write(wc.folder, "sentinel.py", f"open({str(marker)!r}, 'w').write('x')\n" + GOOD)
    _, custom = catalog(wc)
    assert custom["sentinel"]["status"] == "error"
    assert "cannot open files" in custom["sentinel"]["reason"]
    assert custom["sentinel"]["line"] == 1
    assert not marker.exists()
    assert not [
        m
        for m in sys.modules
        if m.startswith(("openchart_custom", "openchart_indicator_files.user"))
    ]


def test_an_edited_file_is_read_again_without_a_restart(wc):
    path = write(wc.folder, "edited.py", GOOD.replace("Middle line", "First title"))
    assert catalog(wc)[1]["edited"]["definition"]["title"] == "First title"
    write(wc.folder, "edited.py", GOOD.replace("Middle line", "Second title"), later_ns=10**9)
    assert catalog(wc)[1]["edited"]["definition"]["title"] == "Second title"
    path.unlink()
    assert "edited" not in catalog(wc)[1]
    assert values(wc, "edited").status_code == 404


def test_a_file_named_like_a_builtin_no_longer_replaces_it(wc):
    write(wc.folder, "ema.py", GOOD)
    body, custom = catalog(wc)
    assert custom["ema"]["status"] == "ok"
    assert custom["ema"]["definition"]["title"] == "Middle line"
    assert [i["title"] for i in body["builtin"]] == ["Moving Average Exponential"]
    flat = [i for i in body["indicators"] if i["id"] == "ema"]
    assert [i["builtin"] for i in flat] == [True]  # the M7 menu keeps the built-in (CI-3: both)
    assert values(wc, "ema").json()["kind"] == "builtin"
    assert values(wc, "ema", kind="custom", params={"length": 3}).json()["values"].keys() == {"mid"}
    (wc.folder / "ema.py").unlink()


def test_broken_files_are_listed_with_their_reason(wc):
    write(wc.folder, "broken.py", "NAME = 'Broken'\nraise RuntimeError('oops')\n")
    write(wc.folder, "no_outputs.py", GOOD.replace("OUTPUTS = [", "OUTPUTS = [] and ["))
    write(wc.folder, "bad_ref.py", GOOD.replace('color="color"', 'color="length"'))
    write(wc.folder, "syntax.py", "NAME = 'S'\ndef compute(bars, params)\n    pass\n")
    write(wc.folder, "Bad-Name.py", GOOD)
    write(wc.folder, "_helper.py", "raise SystemExit('helpers are not indicators')\n")
    body, custom = catalog(wc)
    assert "_helper" not in custom
    assert custom["broken"]["reason"] == "RuntimeError: oops (line 2)"
    assert "OUTPUTS must hold at least one" in custom["no_outputs"]["reason"]
    assert "'length' is not a color parameter" in custom["bad_ref"]["reason"]
    assert custom["syntax"]["reason"].startswith("SyntaxError") and custom["syntax"]["line"] == 2
    assert "lowercase letters" in custom["Bad-Name"]["reason"]
    errors = {e["file"] for e in body["errors"]}
    assert {"broken.py", "no_outputs.py", "bad_ref.py", "syntax.py", "Bad-Name.py"} <= errors
    for name in ("broken", "no_outputs", "bad_ref", "syntax", "Bad-Name", "_helper"):
        (wc.folder / f"{name}.py").unlink()


def test_a_scripts_error_names_its_line_and_the_worker_carries_on(wc):
    write(wc.folder, "divides.py", GOOD.replace("    return {", "    x = 1 / 0\n    return {"))
    out = values(wc, "divides")
    assert out.status_code == 422
    assert out.json()["detail"] == "Mid failed: ZeroDivisionError: division by zero (line 11)"
    write(wc.folder, "mid.py", GOOD)
    assert values(wc, "mid").status_code == 200
    (wc.folder / "divides.py").unlink()


def test_two_files_with_the_same_text_are_two_indicators(wc):
    # The worker caches scripts by hash and gets the source once: the second file must
    # still be compiled under its own name (found in CI-1).
    write(wc.folder, "twin_a.py", GOOD)
    write(wc.folder, "twin_b.py", GOOD)
    _, custom = catalog(wc)
    assert custom["twin_a"]["status"] == custom["twin_b"]["status"] == "ok"
    assert custom["twin_b"]["definition"]["id"] == "twin_b"
    assert values(wc, "twin_b").json()["id"] == "twin_b"
    for name in ("twin_a", "twin_b"):
        (wc.folder / f"{name}.py").unlink()


def test_files_can_use_the_helpers_named_like_builtin_indicators(wc):
    write(wc.folder, "double.py", GOOD.replace("sma", "ema"))
    got = values(wc, "double", params={"length": 3}, **{"from": 0, "limit": 10}).json()
    closes = [b["c"] for b in candles(wc, **{"from": 0, "limit": 10})]
    assert got["values"]["mid"][2] == pytest.approx(sum(closes[:3]) / 3)
    (wc.folder / "double.py").unlink()


# The rest of the server does not wait on a script ------------------------------------------


def test_the_feed_and_alerts_keep_running_while_a_script_hangs(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    (folder / "busy.py").write_text("HANG\n")
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        c.server.indicator_runner.limits = Limits(timeout_s=4)
        assert catalog(c)[1]["busy"]["status"] == "ok"
        alert = c.post(
            "/api/alerts",
            json={
                "instrument": "XAU_USD",
                "type": "level_cross",
                "level": 2000.0,
                "notify": ["app"],
            },
        ).json()

        hung: dict = {}

        def ask():
            t = time.perf_counter()
            hung["response"] = values(c, "busy")
            hung["seconds"] = time.perf_counter() - t

        worker = threading.Thread(target=ask)
        t0 = time.perf_counter()
        with c.websocket_connect("/ws") as ws:
            worker.start()
            time.sleep(0.5)  # the script is hanging now
            assert c.get("/api/health").status_code == 200
            assert (
                c.get("/api/candles", params={"instrument": "XAU_USD", "limit": 5}).status_code
                == 200
            )
            now = time.time()
            c.portal.call(c.server.feed.handle_tick, Tick("XAU_USD", now, 1998.9, 1999.1))
            c.portal.call(c.server.feed.handle_tick, Tick("XAU_USD", now + 1, 2000.9, 2001.1))
            fired = receive_until(ws, "alert_triggered")
            fired_after = time.perf_counter() - t0
        worker.join(15)
        assert fired["alert"]["id"] == alert["id"]
        assert fired_after < 3  # long before the script's deadline
        assert hung["response"].status_code == 422
        assert hung["response"].json()["detail"] == "Fake failed: timed out after 4 s"
        assert hung["seconds"] >= 3.9
