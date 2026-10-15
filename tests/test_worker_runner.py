"""The runner with a fake worker (tests/fake_worker.py): deadlines kill and respawn,
queued jobs retry once, crashes are reported, the event loop stays free while a job
hangs, and three failures in a row quarantine a definition until its file changes."""

import asyncio
import hashlib
import os
import time

import numpy as np
import pandas as pd
import pytest

from openchart.indicators._custom import CustomIndicators
from openchart.indicators.worker.isolation import Isolation
from openchart.indicators.worker.runner import IndicatorRunner, Limits, ScriptFailed
from tests.worker_helpers import FakeLauncher

BARS = {k: np.arange(5, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}


def script(text: str):
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


async def started(timeout_s: float = 1.0):
    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=timeout_s))
    await runner.start()
    return runner, launcher


async def test_a_job_past_its_deadline_is_killed_and_a_new_worker_starts():
    runner, launcher = await started(timeout_s=1.0)
    try:
        for kind in ("HANG", "SLEEP"):
            t = time.perf_counter()
            with pytest.raises(ScriptFailed) as failed:
                await runner.compute(*script(kind), {}, BARS)
            assert failed.value.kind == "timeout"
            assert str(failed.value) == "timed out after 1 s"
            assert time.perf_counter() - t < 2.5
        out = await runner.compute(*script("fine"), {}, BARS)
        assert out["out"].tolist() == [0, 1, 2, 3, 4]
        assert launcher.started == 3 and runner.restarts == 2
        assert runner.state == "ready"
    finally:
        await runner.stop()


async def test_a_queued_job_runs_on_the_new_worker():
    runner, launcher = await started(timeout_s=1.0)
    try:
        hang = asyncio.create_task(runner.compute(*script("HANG"), {}, BARS))
        await asyncio.sleep(0.1)
        queued = asyncio.create_task(runner.compute(*script("queued"), {}, BARS))
        with pytest.raises(ScriptFailed):
            await hang
        assert (await queued)["out"].tolist() == [0, 1, 2, 3, 4]
        assert launcher.started == 2
    finally:
        await runner.stop()


async def test_a_worker_that_died_between_jobs_is_replaced_and_the_job_retried():
    runner, launcher = await started()
    try:
        runner._live.handle.kill()
        runner._live.handle.wait(5000)
        out = await runner.compute(*script("after"), {}, BARS)
        assert out["out"].tolist() == [0, 1, 2, 3, 4]
        assert launcher.started == 2
    finally:
        await runner.stop()


async def test_a_crash_during_a_job_is_reported_with_the_exit_code():
    runner, _ = await started()
    try:
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("CRASH"), {}, BARS)
        assert failed.value.kind == "crash"
        assert "exit code 3" in str(failed.value)
        assert (await runner.compute(*script("ok"), {}, BARS))["out"].size == 5
    finally:
        await runner.stop()


async def test_a_scripts_own_error_keeps_the_worker():
    runner, launcher = await started()
    try:
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("RAISE"), {}, BARS)
        assert failed.value.kind == "error"
        assert str(failed.value) == "ZeroDivisionError: division by zero (line 7)"
        assert not failed.value.counts_for_quarantine
        assert launcher.started == 1
    finally:
        await runner.stop()


async def test_the_event_loop_stays_responsive_while_a_job_hangs():
    runner, _ = await started(timeout_s=1.5)
    gaps = []

    async def ticker():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        with pytest.raises(ScriptFailed):
            await runner.compute(*script("HANG"), {"x": 1}, {k: np.zeros(200_000) for k in BARS})
    finally:
        tick.cancel()
        await runner.stop()
    assert len(gaps) > 50
    assert max(gaps) < 0.25


async def test_stopping_ends_the_worker():
    runner, _ = await started()
    handle = runner._live.handle
    await runner.stop()
    assert not handle.alive()


# Quarantine (the registry's) -------------------------------------------------------------


@pytest.fixture
async def custom(tmp_path):
    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=0.5))
    await runner.start()
    registry = CustomIndicators(tmp_path)
    registry.worker_ready(runner, Isolation("process-only", False, "test"))
    yield registry, launcher, tmp_path
    await runner.stop()


FRAME = pd.DataFrame(BARS)


async def test_three_failures_in_a_row_quarantine_until_the_file_changes(custom):
    registry, launcher, folder = custom
    (folder / "busy.py").write_text("HANG\n")
    d = await registry.get("busy")
    assert d.status == "ok"
    for _ in range(3):
        with pytest.raises(ScriptFailed) as failed:
            await registry.compute(d, FRAME, {})
        assert failed.value.kind == "timeout"
    assert d.status == "quarantined" and "stopped after 3 failures" in d.reason
    starts = launcher.started
    with pytest.raises(ScriptFailed) as failed:  # not sent to the worker again
        await registry.compute(await registry.get("busy"), FRAME, {})
    assert failed.value.kind == "quarantined"
    assert launcher.started == starts

    (folder / "busy.py").write_text("fixed\n")
    st = (folder / "busy.py").stat()
    os.utime(folder / "busy.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    d = await registry.get("busy")
    assert d.status == "ok" and d.failures == 0
    assert (await registry.compute(d, FRAME, {}))["out"].tolist() == [0, 1, 2, 3, 4]


async def test_a_success_resets_the_count_and_errors_do_not_count(custom):
    registry, _, folder = custom
    (folder / "raises.py").write_text("RAISE\n")
    d = await registry.get("raises")
    for _ in range(4):
        with pytest.raises(ScriptFailed):
            await registry.compute(d, FRAME, {})
    assert d.status == "ok" and d.failures == 0  # its own exception: shown, not counted


def test_probe_targets_are_absolute_whatever_the_data_folder_is_given_as(monkeypatch, tmp_path):
    # The worker runs in another working folder: a relative data folder made every probe
    # report "absent" (found in CI-1).
    from pathlib import Path

    from openchart.indicators.worker.isolation import probe_targets

    monkeypatch.chdir(tmp_path)
    targets = probe_targets(Path("data"), 9, "restricted")
    assert Path(targets["read"]["read the database"]) == tmp_path / "data" / "openchart.db"
    assert Path(targets["list"]["list the data folder"]).is_absolute()


def counting(registry):
    calls = []
    real = registry.runner.compute

    async def compute(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    registry.runner.compute = compute
    return calls


async def test_an_error_is_answered_from_memory_for_the_same_script_params_and_bars(custom):
    # Owner, CI-1: errors are not counted toward the quarantine, so they must not keep
    # the worker busy either.
    registry, _, folder = custom
    (folder / "raises.py").write_text("RAISE\n")
    d = await registry.get("raises")
    calls = counting(registry)
    for _ in range(3):
        with pytest.raises(ScriptFailed, match="ZeroDivisionError"):
            await registry.compute(d, FRAME, {})
    assert len(calls) == 1
    with pytest.raises(ScriptFailed):  # other bars: computed
        await registry.compute(d, FRAME.assign(c=FRAME["c"] + 1), {})
    assert len(calls) == 2


async def test_a_slow_error_pauses_that_script_until_its_file_changes(custom):
    registry, _, folder = custom  # deadline 0.5 s; the script raises after 0.4 s
    (folder / "slow.py").write_text("SLOWRAISE\n")
    d = await registry.get("slow")
    calls = counting(registry)
    for shift in range(3):  # new bars each time, as a forming bar gives
        with pytest.raises(ScriptFailed) as failed:
            await registry.compute(d, FRAME.assign(c=FRAME["c"] + shift), {})
        assert failed.value.kind == "error"
    assert len(calls) == 1
    assert d.status == "ok" and d.failures == 0  # shown, not counted

    (folder / "slow.py").write_text("fixed\n")
    st = (folder / "slow.py").stat()
    os.utime(folder / "slow.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    d = await registry.get("slow")
    assert (await registry.compute(d, FRAME, {}))["out"].tolist() == [0, 1, 2, 3, 4]
    assert len(calls) == 2


async def test_the_deadline_still_applies_to_every_job(custom):
    registry, _, folder = custom
    (folder / "busy.py").write_text("HANG\n")
    d = await registry.get("busy")
    t = time.perf_counter()
    with pytest.raises(ScriptFailed) as failed:
        await registry.compute(d, FRAME, {})
    assert failed.value.kind == "timeout" and time.perf_counter() - t < 2


async def test_an_isolated_level_that_fails_its_probes_never_falls_back(tmp_path):
    """D7 (a): when the AppContainer (or the sidecar) does not meet the minimum, custom
    code does not run at all; the launchers after it are never started."""
    from openchart.indicators.worker.isolation import start_worker

    class Isolated(FakeLauncher):
        level = "appcontainer"

    isolated, weaker = Isolated(), FakeLauncher()
    (tmp_path / "openchart.db").write_bytes(b"x")
    runner, iso = await start_worker(tmp_path, Limits(), [isolated, weaker])
    assert runner is None and iso.level == "unavailable"
    assert "could read the database" in iso.reason
    assert isolated.started == 1 and weaker.started == 0
