"""When the save-time checks run (CI-2 step c), with the fake worker.

A check is two worker jobs (the lookahead run and the warmup run) at the lowest priority:
never ahead of a chart request, one check at a time, not before a pause after the worker
starts. Each version of a file is checked for at most five settings. A check that times
out is reported, never counted toward the quarantine.
"""

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

N = 200
FRAME = pd.DataFrame(
    {
        "ts": 1_760_450_400.0 + 300 * np.arange(N),
        "o": 2000.0 + np.arange(N),
        "h": 2001.0 + np.arange(N),
        "l": 1999.0 + np.arange(N),
        "c": 2000.5 + np.sin(np.arange(N) / 7) * 10,
        "v": np.ones(N),
    }
)
BARS = {c: FRAME[c].to_numpy() for c in FRAME}


def script(text):
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


def colour(i):
    return {"color": f"#00000{i}", "width": 1}


async def make(tmp_path, timeout_s=1.0, stagger=0.0, gap=0.0):
    runner = IndicatorRunner(FakeLauncher(), Limits(timeout_s=timeout_s))
    await runner.start()
    registry = CustomIndicators(tmp_path)
    registry.check_stagger, registry.check_gap = stagger, gap
    registry.worker_ready(runner, Isolation("process-only", False, "test"))
    return registry, runner


@pytest.fixture
async def custom(tmp_path):
    registry, runner = await make(tmp_path)
    yield registry, tmp_path
    await registry.close()
    await runner.stop()


async def settled(registry, d, params, seconds=10.0):
    end = time.monotonic() + seconds
    while True:
        got = registry.checks_for(d, params)
        if got and "pending" not in (got["lookahead"], got["warmup"]):
            return got
        assert time.monotonic() < end, got
        await asyncio.sleep(0.02)


def edit(folder, name, text):
    path = folder / name
    path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


# Priority -------------------------------------------------------------------------------


async def test_a_chart_request_waits_for_at_most_one_check_job(tmp_path):
    _, runner = await make(tmp_path, timeout_s=5)
    try:
        slow = script("PAUSEMS 300\n")
        await runner.compute(*slow, {}, BARS)  # the worker learns the source
        done = []

        async def job(name, priority):
            await runner.compute(*slow, {}, BARS, priority=priority)
            done.append(name)

        checks = [asyncio.create_task(job(f"check{i}", "low")) for i in range(3)]
        await asyncio.sleep(0.1)  # check0 is running
        t = time.monotonic()
        await asyncio.gather(job("chart1", "normal"), job("chart2", "normal"))
        waited = time.monotonic() - t
        await asyncio.gather(*checks)
        assert done == ["check0", "chart1", "chart2", "check1", "check2"]
        assert waited < 0.3 * 3 + 0.3  # the rest of check0, then the two charts
    finally:
        await runner.stop()


# What is checked, and keyed how ----------------------------------------------------------


async def test_a_lookahead_script_is_warned_about_and_a_clean_one_is_not(custom):
    registry, folder = custom
    (folder / "ahead.py").write_text("SHIFT\n")
    (folder / "clean.py").write_text("fine\n")
    ahead, clean = await registry.get("ahead"), await registry.get("clean")
    await registry.compute(ahead, FRAME, colour(1), warmup=0)
    await registry.compute(clean, FRAME, colour(1), warmup=0)
    got = await settled(registry, ahead, colour(1))
    assert got["lookahead"] == "warned"
    (f,) = got["findings"]
    assert (f["check"], f["level"], f["output"]) == ("lookahead", "warning", "out")
    assert f["t"] == FRAME["ts"][N - 50 - 1]
    assert got["params"] == colour(1) and got["bars"] == N
    assert got["version"] == ahead.digest[:12]
    ok = await settled(registry, clean, colour(1))
    assert (ok["lookahead"], ok["warmup"], ok["findings"]) == ("ok", "ok", [])
    assert ahead.to_json()["checks"] == [got]


async def test_each_version_is_checked_for_at_most_five_settings(custom):
    registry, folder = custom
    (folder / "clean.py").write_text("fine\n")
    d = await registry.get("clean")
    for i in range(6):
        await registry.compute(d, FRAME, colour(i), warmup=0)
    for i in range(5):
        assert (await settled(registry, d, colour(i)))["lookahead"] == "ok"
    sixth = registry.checks_for(d, colour(5))
    assert sixth["lookahead"] == sixth["warmup"] == "not_checked"
    assert "not checked for these settings" in sixth["message"]
    assert len(d.to_json()["checks"]) == 5  # the five results are kept


async def test_settings_already_checked_are_not_checked_again(custom):
    registry, folder = custom
    (folder / "clean.py").write_text("fine\n")
    d = await registry.get("clean")
    await registry.compute(d, FRAME, colour(1), warmup=0)
    await settled(registry, d, colour(1))
    calls = []
    real = registry.runner.compute

    async def counting(*args, **kwargs):
        calls.append(kwargs.get("priority", "normal"))
        return await real(*args, **kwargs)

    registry.runner.compute = counting
    for _ in range(3):
        await registry.compute(d, FRAME.assign(c=FRAME["c"] + 1), colour(1), warmup=0)
    await asyncio.sleep(0.3)
    assert calls == ["normal"] * 3


async def test_a_checks_two_runs_are_jobs_at_the_lowest_priority(custom):
    registry, folder = custom
    (folder / "clean.py").write_text("fine\n")
    d = await registry.get("clean")
    calls = []
    real = registry.runner.compute

    async def counting(*args, **kwargs):
        calls.append((len(args[4]["c"]), kwargs.get("priority", "normal")))
        return await real(*args, **kwargs)

    registry.runner.compute = counting
    await registry.compute(d, FRAME, colour(1), warmup=0)
    await settled(registry, d, colour(1))
    # The chart's 200 bars, then the lookahead run without the last 50, then the warmup
    # run from bar 50.
    assert calls == [(N, "normal"), (N - 50, "low"), (N - 50, "low")]


async def test_a_new_version_starts_its_checks_over(custom):
    registry, folder = custom
    (folder / "x.py").write_text("SHIFT\n")
    d = await registry.get("x")
    await registry.compute(d, FRAME, colour(1), warmup=0)
    assert (await settled(registry, d, colour(1)))["lookahead"] == "warned"
    edit(folder, "x.py", "fine\n")
    d2 = await registry.get("x")
    assert d2.to_json()["checks"] == []
    await registry.compute(d2, FRAME, colour(1), warmup=0)
    assert (await settled(registry, d2, colour(1)))["lookahead"] == "ok"


# Timeouts and the time budget --------------------------------------------------------------


async def test_a_check_that_times_out_never_counts_toward_the_quarantine(tmp_path):
    registry, runner = await make(tmp_path, timeout_s=0.5)
    try:
        # The chart's 200 bars are fine; the lookahead check's 150 loop forever.
        (tmp_path / "stall.py").write_text("STALLBELOW 190\n")
        d = await registry.get("stall")
        for i in range(4):
            await registry.compute(d, FRAME, colour(i), warmup=0)
            got = await settled(registry, d, colour(i))
            assert got["lookahead"] == "timeout"
            assert got["warmup"] == "skipped"
            assert any("timed out" in f["message"] for f in got["findings"])
        assert d.status == "ok" and d.failures == 0
        assert (await registry.compute(d, FRAME, colour(9)))["out"].size == N
    finally:
        await registry.close()
        await runner.stop()


async def test_a_check_run_that_ends_the_worker_skips_the_second_run(tmp_path):
    registry, runner = await make(tmp_path, timeout_s=2)
    try:
        # The lookahead run (150 bars) ends the worker; the warmup run (also 150 bars)
        # would end the new one too, so it is not started.
        (tmp_path / "end.py").write_text("ENDBELOW 190\n")
        d = await registry.get("end")
        await registry.compute(d, FRAME, colour(1), warmup=0)
        got = await settled(registry, d, colour(1))
        assert (got["lookahead"], got["warmup"]) == ("failed", "skipped")
        assert "the worker ended" in got["findings"][0]["message"]
        assert runner.restarts == 1
        assert d.status == "ok" and d.failures == 0
    finally:
        await registry.close()
        await runner.stop()


async def test_a_run_that_cannot_fit_in_the_deadline_is_skipped_not_started(tmp_path):
    registry, runner = await make(tmp_path, timeout_s=2.5)
    try:
        (tmp_path / "slow.py").write_text("PAUSEMS 2250\n")  # over 80% of 2.5 s
        d = await registry.get("slow")
        calls = []
        real = runner.compute

        async def counting(*args, **kwargs):
            calls.append(kwargs.get("priority", "normal"))
            return await real(*args, **kwargs)

        runner.compute = counting
        await registry.compute(d, FRAME, colour(1), warmup=0)
        got = await settled(registry, d, colour(1))
        assert (got["lookahead"], got["warmup"]) == ("skipped", "skipped")
        assert any("must fit in 2.0 s" in f["message"] for f in got["findings"])
        assert calls == ["normal"]
        assert runner.restarts == 0
    finally:
        await registry.close()
        await runner.stop()


# Staggering --------------------------------------------------------------------------------


async def test_checks_wait_after_the_worker_starts_and_run_one_at_a_time(tmp_path):
    registry, runner = await make(tmp_path, timeout_s=5, stagger=0.6, gap=0.3)
    try:
        (tmp_path / "clean.py").write_text("fine\n")
        d = await registry.get("clean")
        for i in range(3):
            await registry.compute(d, FRAME, colour(i), warmup=0)
        finished = {}
        end = time.monotonic() + 10
        while len(finished) < 3:
            for i in range(3):
                got = registry.checks_for(d, colour(i))
                if (
                    i not in finished
                    and got["lookahead"] != "pending"
                    and got["warmup"] != "pending"
                ):
                    finished[i] = time.monotonic()
            assert time.monotonic() < end
            await asyncio.sleep(0.01)
        times = sorted(finished.values())
        assert times[0] >= runner.ready_at + 0.6
        assert times[1] - times[0] >= 0.3 and times[2] - times[1] >= 0.3
    finally:
        await registry.close()
        await runner.stop()


async def test_a_restarting_worker_holds_the_checks_and_pauses_them_again(tmp_path):
    registry, runner = await make(tmp_path, timeout_s=0.5, stagger=0.6)
    try:
        (tmp_path / "clean.py").write_text("fine\n")
        (tmp_path / "busy.py").write_text("HANG\n")
        d, busy = await registry.get("clean"), await registry.get("busy")
        await registry.compute(d, FRAME, colour(1), warmup=0)  # a check waits for the pause
        with pytest.raises(ScriptFailed):  # a timeout: the worker is replaced, not restarted
            await registry.compute(busy, FRAME, colour(1))
        await asyncio.sleep(1.0)  # well past the first pause
        assert registry.checks_for(d, colour(1))["lookahead"] == "pending"  # not started
        await registry.compute(d, FRAME, colour(2), warmup=0)  # a chart request: a new worker
        restarted = time.monotonic()
        assert runner.state == "ready"
        await settled(registry, d, colour(1))
        assert time.monotonic() >= restarted + 0.5  # the pause again, from the new worker
    finally:
        await registry.close()
        await runner.stop()
