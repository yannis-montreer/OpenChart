"""Hardening after the security review (docs/security-review-custom-indicators.md) and
Session A's review of CI-2 and CI-3: a bad frame from the worker and the quarantine, the
save-time check after a request with too few bars, `from`/`to`/`limit` out of range,
an all-or-nothing delete, and an import that brings nothing."""

import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators._custom import CustomIndicators
from openchart.indicators.worker.isolation import Isolation
from openchart.indicators.worker.runner import IndicatorRunner, Limits, ScriptFailed
from openchart.settings import INDICATOR_EDITING
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import wait_for_worker, write
from tests.worker_helpers import FakeLauncher

BARS = {k: np.arange(5, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}
FRAME = pd.DataFrame(BARS)
ISOLATED = Isolation("appcontainer", True, "minimum isolation met")


# S2: a bad frame kills the worker at once; the quarantine counts it only when it repeats


@pytest.fixture
async def custom(tmp_path):
    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=3))
    await runner.start()
    registry = CustomIndicators(tmp_path)
    registry.worker_ready(runner, Isolation("process-only", False, "test"))
    yield registry, launcher, tmp_path, runner
    await runner.stop()


async def test_a_bad_frame_fails_the_job_at_once_as_a_protocol_error(custom):
    registry, launcher, folder, _runner = custom
    (folder / "deep.py").write_text("DEEPHEADER\n")
    d = await registry.get("deep")
    t = time.perf_counter()
    with pytest.raises(ScriptFailed) as failed:
        await registry.compute(d, FRAME, {})
    assert failed.value.kind == "protocol"
    assert time.perf_counter() - t < 1.5  # not the 3 s deadline
    assert "not a frame" in str(failed.value)
    # The worker was replaced: the next job runs on a new one.
    (folder / "fine.py").write_text("fine\n")
    fine = await registry.get("fine")
    assert (await registry.compute(fine, FRAME, {}))["out"].tolist() == [0, 1, 2, 3, 4]
    assert launcher.started == 2


async def test_a_script_that_breaks_the_stream_after_its_answer_cannot_quarantine_another(custom):
    """The bad frame of `attack` arrives during the next job, `victim`'s. That job fails,
    but `victim` ran on a worker another script had used, so it is not counted: three
    rounds do not stop it (before, three timeouts did)."""
    registry, _launcher, folder, _runner = custom
    (folder / "attack.py").write_text("DEEPHEADERAFTER\n")
    (folder / "victim.py").write_text("victim\n")
    attack, victim = await registry.get("attack"), await registry.get("victim")
    for _ in range(4):
        await registry.compute(attack, FRAME, {})
        try:
            await registry.compute(victim, FRAME, {})
        except ScriptFailed as exc:
            assert exc.kind == "protocol" and not exc.counts_for_quarantine
    assert victim.status == "ok" and victim.failures == 0


async def test_a_script_that_breaks_the_stream_itself_is_quarantined_after_three(custom):
    """Each bad frame replaces the worker, so the next try is the first job of a new
    worker, where no other script has run: that one counts."""
    registry, _launcher, folder, _runner = custom
    (folder / "deep.py").write_text("DEEPHEADER\n")
    d = await registry.get("deep")
    for _ in range(3):
        with pytest.raises(ScriptFailed) as failed:
            await registry.compute(d, FRAME, {})
        assert failed.value.kind == "protocol" and failed.value.counts_for_quarantine
    assert d.status == "quarantined"


async def test_a_bad_frame_after_other_jobs_is_not_counted(custom):
    registry, _launcher, folder, _runner = custom
    (folder / "fine.py").write_text("fine\n")
    (folder / "deep.py").write_text("DEEPHEADER\n")
    fine, deep = await registry.get("fine"), await registry.get("deep")
    await registry.compute(fine, FRAME, {})  # the worker has run another script
    with pytest.raises(ScriptFailed) as failed:
        await registry.compute(deep, FRAME, {})
    assert failed.value.kind == "protocol" and not failed.value.counts_for_quarantine
    assert deep.failures == 0


# A server with the fake worker ------------------------------------------------------------


@pytest.fixture
def fc(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder = harness.server, folder
        c.server.custom.check_stagger = c.server.custom.check_gap = 0.0
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def values(c, indicator, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": "{}", **query}
    return c.get(f"/api/indicators/{indicator}", params=q)


def settled(c, indicator, **query):
    end = time.monotonic() + 30
    while True:
        r = values(c, indicator, **query)
        assert r.status_code == 200, r.text
        got = r.json()["checks"]
        if "pending" not in (got["lookahead"], got["warmup"]):
            return got
        assert time.monotonic() < end, got
        time.sleep(0.05)


def test_a_check_skipped_for_too_few_bars_runs_again_on_a_full_request(fc):
    """Session A's finding 2: after an edit, the browser's first request is the two-bar
    refresh; the check was then "skipped: too few bars" for good, and a script that
    looks ahead got no warning."""
    (fc.folder / "ahead.py").write_text("SHIFT\n")
    t = bar_times(fc)
    short = settled(fc, "ahead", **{"from": t[-2], "to": t[-1] + 1})
    assert short["bars"] < 8 and short["lookahead"] == "skipped"
    full = settled(fc, "ahead", limit=300)
    assert full["bars"] == 300 and full["lookahead"] == "warned"


def test_a_check_is_not_run_again_for_a_request_that_adds_a_few_bars(fc):
    """Bounded: a rerun needs at least twice the bars the skipped check had."""
    (fc.folder / "ahead.py").write_text("SHIFT\n")
    t = bar_times(fc)
    short = settled(fc, "ahead", **{"from": t[-4], "to": t[-1] + 1})
    assert short["lookahead"] == "skipped"
    again = settled(fc, "ahead", **{"from": t[-6], "to": t[-1] + 1})  # 6 bars, not 2 x 4
    assert again == short


def bar_times(c):
    """The times of the latest 300 bars, without asking the indicator (that would check it)."""
    q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 300}
    return [b["t"] for b in c.get("/api/candles", params=q).json()["candles"]]


@pytest.mark.parametrize(
    "query",
    [
        {"from": 2**63},
        {"to": 2**63},
        {"from": -(2**63) - 1},
        {"limit": 2**63},
        {"from": 10**30},
    ],
)
def test_from_to_and_limit_out_of_range_are_refused(fc, query):
    """Session A's finding 3: SQLite cannot bind them, which was a 500."""
    for path in ("/api/candles", "/api/indicators/ema"):
        q = {"instrument": "XAU_USD", "timeframe": "5m", **query}
        r = fc.get(path, params=q)
        assert r.status_code == 422, (path, r.status_code, r.text[:200])


def test_from_and_to_still_take_any_time_in_range(fc):
    for q in ({"from": 0, "to": 2**53}, {"from": -10, "limit": 5}):
        r = fc.get("/api/candles", params={"instrument": "XAU_USD", "timeframe": "5m", **q})
        assert r.status_code == 200, r.text


# The import that brings nothing ------------------------------------------------------------


def chart_url(c, instrument="XAU_USD"):
    return f"/api/charts/{c.server.source}/{instrument}/indicators"


@pytest.mark.parametrize("old", [[], [{"id": "vanished"}], [{"id": "ema", "params": {"a": [1]}}]])
def test_an_import_that_brings_nothing_does_not_set_the_chart(fc, old):
    """An empty legacy list (or one with nothing usable) must not use up the chart's one
    import: another browser with a real list comes later."""
    r = fc.post(chart_url(fc) + "/import", json={"indicators": old})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 0 and r.json()["indicators"] == []
    assert r.json()["imported"] is False
    assert fc.get(chart_url(fc)).json()["revision"] == 0
    later = fc.post(chart_url(fc) + "/import", json={"indicators": [{"id": "ema"}]}).json()
    assert later["revision"] == 1 and later["imported"] is True
    assert [i["id"] for i in later["indicators"]] == ["ema"]


# An all-or-nothing delete ---------------------------------------------------------------------


def allow_editing(c):
    c.server.custom.isolation = ISOLATED
    c.portal.call(c.server.settings.set, INDICATOR_EDITING, "on")


def in_use(c):
    write(c.folder, "mine.py", "PLAIN\n")
    history = c.folder / ".history" / "mine"
    history.mkdir(parents=True)
    (history / "20261014T101502Z.py").write_text("PLAIN old\n")
    r = c.put(chart_url(c), json={"indicators": [{"id": "mine"}, {"id": "ema"}]})
    assert r.status_code == 200, r.text


def test_a_delete_whose_database_step_fails_leaves_everything_as_it_was(fc, monkeypatch):
    """Session A's review of #33: the instances were removed before the file moved, so a
    failure in between left them out of step. Now the files move first and move back if
    the database step fails."""
    allow_editing(fc)
    in_use(fc)
    from openchart.store import chart_indicators as store_module

    async def broken(self, *args, **kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(store_module.ChartIndicatorStore, "replace", broken)
    with pytest.raises(RuntimeError):
        fc.delete("/api/custom-indicators/mine?confirm=true")
    assert (fc.folder / "mine.py").read_text() == "PLAIN\n"
    assert (fc.folder / ".history" / "mine" / "20261014T101502Z.py").is_file()
    assert [p for p in (fc.folder / ".deleted").iterdir() if any(p.iterdir())] == []
    monkeypatch.undo()
    assert [i["id"] for i in fc.get(chart_url(fc)).json()["indicators"]] == ["mine", "ema"]


def test_a_delete_that_fails_on_the_second_chart_puts_the_first_back(fc, monkeypatch):
    allow_editing(fc)
    in_use(fc)
    from openchart.market.instruments import FX_HOURS
    from tests.conftest import EURO

    fc.portal.call(fc.server.store.upsert_instrument, fc.server.source, EURO, FX_HOURS)
    r = fc.put(chart_url(fc, "EUR_USD"), json={"indicators": [{"id": "mine"}]})
    assert r.status_code == 200
    from openchart.store import chart_indicators as store_module

    real, calls = store_module.ChartIndicatorStore.replace, []

    async def second_fails(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("the disk is full")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(store_module.ChartIndicatorStore, "replace", second_fails)
    with pytest.raises(RuntimeError):
        fc.delete("/api/custom-indicators/mine?confirm=true")
    monkeypatch.undo()
    assert [i["id"] for i in fc.get(chart_url(fc)).json()["indicators"]] == ["mine", "ema"]
    assert [i["id"] for i in fc.get(chart_url(fc, "EUR_USD")).json()["indicators"]] == ["mine"]
    assert (fc.folder / "mine.py").is_file()


def test_a_delete_whose_history_move_fails_leaves_everything_as_it_was(fc, monkeypatch):
    allow_editing(fc)
    in_use(fc)
    from openchart.api import routes_custom_indicators as module

    real = module.os.replace

    def replace(src, dst):
        if str(dst).endswith("history"):
            raise PermissionError("the history folder is open in another program")
        return real(src, dst)

    monkeypatch.setattr(module.os, "replace", replace)
    r = fc.delete("/api/custom-indicators/mine?confirm=true")
    assert r.status_code == 409 and "history" in r.json()["detail"]
    monkeypatch.undo()
    assert (fc.folder / "mine.py").read_text() == "PLAIN\n"
    assert (fc.folder / ".history" / "mine" / "20261014T101502Z.py").is_file()
    assert [i["id"] for i in fc.get(chart_url(fc)).json()["indicators"]] == ["mine", "ema"]


def test_a_delete_that_works_is_unchanged(fc):
    allow_editing(fc)
    in_use(fc)
    body = fc.delete("/api/custom-indicators/mine?confirm=true").json()
    moved = fc.folder / body["moved_to"]
    assert (moved / "mine.py").read_text() == "PLAIN\n"
    assert (moved / "history" / "20261014T101502Z.py").is_file()
    assert [i["id"] for i in fc.get(chart_url(fc)).json()["indicators"]] == ["ema"]


# S1 through the API ---------------------------------------------------------------------------


def test_the_catalog_says_how_to_turn_editing_on(fc):
    fc.server.custom.isolation = ISOLATED
    editing = fc.get("/api/indicators").json()["editing"]
    assert editing["setting"] == "auto" and editing["allowed"] is False
    assert "openchart settings set indicator-editing on" in editing["reason"]
    fc.portal.call(fc.server.settings.set, INDICATOR_EDITING, "on")
    assert fc.get("/api/indicators").json()["editing"]["allowed"] is True
