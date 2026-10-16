"""When the save-time checks run (CI-2 step c), with the fake worker.

A check is two worker jobs (the lookahead run and the warmup run) at the lowest priority:
never ahead of a chart request, one check at a time, not before a pause after the worker
starts. Each version of a file is checked for at most five settings. A check that times
out is reported, never counted toward the quarantine.

Nothing here depends on how fast the machine is. Pauses, spacing and the time budget run
on a clock the test moves by hand (`ManualClock`); the order of jobs is held with a gate
in the fake worker. Only the deadline tests use real time, because the deadline is real:
their scripts loop forever or end the worker, so any deadline is passed or never reached.
"""

import asyncio
import hashlib
import os
import time

import numpy as np
import pandas as pd
import pytest

from openchart.indicators._custom import CHECK_GAP, CHECK_STAGGER, CustomIndicators
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
REAL_WAIT = 20.0  # seconds of real time any wait below may take before the test fails


class ManualClock:
    """Time that moves only when the test says so. `sleep` waits until it has moved."""

    def __init__(self):
        self.now = 1000.0
        self._moved = asyncio.Event()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._moved.set()

    async def sleep(self, seconds: float) -> None:
        until = self.now + seconds
        while self.now < until:
            self._moved.clear()
            await self._moved.wait()


def script(text):
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


def colour(i):
    return {"color": f"#00000{i}", "width": 1}


async def make(tmp_path, timeout_s=2.0, stagger=0.0, gap=0.0):
    clock = ManualClock()
    runner = IndicatorRunner(FakeLauncher(), Limits(timeout_s=timeout_s), clock=clock)
    await runner.start()
    registry = CustomIndicators(tmp_path)
    registry.check_stagger, registry.check_gap = stagger, gap
    registry.sleep = clock.sleep
    registry.worker_ready(runner, Isolation("process-only", False, "test"))
    return registry, runner, clock


@pytest.fixture
async def custom(tmp_path):
    registry, runner, clock = await make(tmp_path)
    registry.clock_for_tests = clock
    yield registry, tmp_path
    await registry.close()
    await runner.stop()


def done(got):
    return got is not None and "pending" not in (got["lookahead"], got["warmup"])


async def settled(registry, d, params, clock=None, step=0.25):
    """Wait (in real time) for the check of these settings, moving the clock as it goes."""
    clock = clock or registry.clock_for_tests
    end = time.monotonic() + REAL_WAIT
    while not done(got := registry.checks_for(d, params)):
        assert time.monotonic() < end, got
        clock.advance(step)
        await asyncio.sleep(0.005)
    return got


async def until(condition, what):
    end = time.monotonic() + REAL_WAIT
    while not condition():
        assert time.monotonic() < end, what
        await asyncio.sleep(0.005)


def edit(folder, name, text):
    path = folder / name
    path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def counting(runner, calls, *, bars=False):
    real = runner.compute

    async def compute(*args, **kwargs):
        priority = kwargs.get("priority", "normal")
        calls.append((len(args[4]["c"]), priority) if bars else priority)
        return await real(*args, **kwargs)

    runner.compute = compute


# Priority -------------------------------------------------------------------------------


async def test_a_chart_request_waits_for_at_most_one_check_job(tmp_path):
    _, runner, _ = await make(tmp_path)
    gate = tmp_path / "gate"
    gate.mkdir()
    held = script(f"GATE {gate}\n")
    try:
        (gate / "go").write_text("")
        await runner.compute(*held, {}, BARS)  # the worker learns the source
        (gate / "go").unlink()
        finished = []

        async def job(name, priority):
            await runner.compute(*held, {}, BARS, priority=priority)
            finished.append(name)

        checks = [asyncio.create_task(job(f"check{i}", "low")) for i in range(3)]
        await until(lambda: len(list(gate.glob("started-*"))) == 2, "check0 to start")
        charts = [asyncio.create_task(job(f"chart{i}", "normal")) for i in (1, 2)]
        await until(lambda: runner._normal_waiting == 2, "both charts to queue")
        (gate / "go").write_text("")
        await asyncio.gather(*checks, *charts)
        # check0 was running; both charts went before the two checks still queued.
        assert finished == ["check0", "chart1", "chart2", "check1", "check2"]
    finally:
        (gate / "go").write_text("")
        await runner.stop()


async def test_the_run_time_counts_neither_the_queue_nor_a_worker_start(tmp_path):
    _, runner, clock = await make(tmp_path)
    real_launch = runner._launch

    async def slow_start():
        live = await real_launch()
        clock.advance(30)  # a worker that took 30 s to start
        return live

    runner._launch = slow_start
    try:
        runner.kill_for_tests()
        await until(lambda: not runner.up, "the worker to end")  # else the job may reach it
        await runner.compute(*script("fine\n"), {}, BARS)  # starts a new worker first
        assert runner.restarts == 1
        assert runner.last_job_s == 0.0  # the exchange itself took no time on this clock
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
    counting(registry.runner, calls)
    for _ in range(3):
        await registry.compute(d, FRAME.assign(c=FRAME["c"] + 1), colour(1), warmup=0)
    for _ in range(20):  # time passes; nothing else is sent
        registry.clock_for_tests.advance(1)
        await asyncio.sleep(0.005)
    assert calls == ["normal"] * 3
    assert not registry._pending


async def test_a_checks_two_runs_are_jobs_at_the_lowest_priority(custom):
    registry, folder = custom
    (folder / "clean.py").write_text("fine\n")
    d = await registry.get("clean")
    calls = []
    counting(registry.runner, calls, bars=True)
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


# Deadlines (real time) and the time budget (the manual clock) ------------------------------


async def test_a_check_that_times_out_never_counts_toward_the_quarantine(tmp_path):
    registry, runner, clock = await make(tmp_path, timeout_s=2.0)
    try:
        # The chart's 200 bars are fine; the lookahead check's 150 loop forever.
        (tmp_path / "stall.py").write_text("STALLBELOW 190\n")
        d = await registry.get("stall")
        for i in range(4):
            await registry.compute(d, FRAME, colour(i), warmup=0)
            got = await settled(registry, d, colour(i), clock)
            assert got["lookahead"] == "timeout"
            assert got["warmup"] == "skipped"
            assert any("timed out" in f["message"] for f in got["findings"])
        assert d.status == "ok" and d.failures == 0
        assert (await registry.compute(d, FRAME, colour(9)))["out"].size == N
    finally:
        await registry.close()
        await runner.stop()


async def test_a_check_run_that_ends_the_worker_skips_the_second_run(tmp_path):
    registry, runner, clock = await make(tmp_path)
    try:
        # The lookahead run (150 bars) ends the worker; the warmup run (also 150 bars)
        # would end the new one too, so it is not started.
        (tmp_path / "end.py").write_text("ENDBELOW 190\n")
        d = await registry.get("end")
        await registry.compute(d, FRAME, colour(1), warmup=0)
        got = await settled(registry, d, colour(1), clock)
        assert (got["lookahead"], got["warmup"]) == ("failed", "skipped")
        assert "the worker ended" in got["findings"][0]["message"]
        assert runner.restarts == 1
        assert d.status == "ok" and d.failures == 0
    finally:
        await registry.close()
        await runner.stop()


def charged(runner, clock, seconds):
    """Each exchange with the worker takes the next of `seconds` on the manual clock."""
    real_ask = runner._ask
    left = list(seconds)

    async def ask(*args, **kwargs):
        got = await real_ask(*args, **kwargs)
        if args[1].get("cmd") == "compute":
            clock.advance(left.pop(0))
        return got

    runner._ask = ask


@pytest.mark.parametrize(
    ("seconds", "states", "sent"),
    [
        ((2.25,), ("skipped", "skipped"), ["normal"]),  # the chart's own run: over 2.0 s
        ((1.5, 2.25), ("ok", "skipped"), ["normal", "low"]),  # the lookahead run: over
        ((2.0, 2.0, 2.0), ("ok", "ok"), ["normal", "low", "low"]),  # exactly 80%: fits
    ],
)
async def test_a_run_that_cannot_fit_in_the_deadline_is_skipped_not_started(
    tmp_path, seconds, states, sent
):
    registry, runner, clock = await make(tmp_path, timeout_s=2.5)
    try:
        (tmp_path / "slow.py").write_text("fine\n")
        d = await registry.get("slow")
        calls = []
        counting(runner, calls)
        charged(runner, clock, seconds)
        await registry.compute(d, FRAME, colour(1), warmup=0)
        got = await settled(registry, d, colour(1), clock, step=0)
        assert (got["lookahead"], got["warmup"]) == states
        if "skipped" in states:
            assert any("must fit in 2.0 s" in f["message"] for f in got["findings"])
        assert calls == sent
        assert runner.restarts == 0
    finally:
        await registry.close()
        await runner.stop()


# Pauses ------------------------------------------------------------------------------------


async def test_checks_wait_after_the_worker_starts_and_run_one_at_a_time(tmp_path):
    registry, runner, clock = await make(tmp_path, stagger=CHECK_STAGGER, gap=CHECK_GAP)
    try:
        (tmp_path / "clean.py").write_text("fine\n")
        d = await registry.get("clean")
        for i in range(3):
            await registry.compute(d, FRAME, colour(i), warmup=0)
        clock.advance(CHECK_STAGGER - 0.5)
        await asyncio.sleep(0.2)
        assert not registry.check_log  # still within the pause after the start
        for i in range(3):
            await settled(registry, d, colour(i), clock, step=0.1)
        (s0, e0), (s1, e1), (s2, _) = [(s, e) for _, s, e in registry.check_log]
        assert s0 >= runner.ready_at + CHECK_STAGGER
        assert s1 >= e0 + CHECK_GAP and s2 >= e1 + CHECK_GAP
    finally:
        await registry.close()
        await runner.stop()


async def test_a_worker_that_is_down_holds_the_checks_and_pauses_them_again(tmp_path):
    registry, runner, clock = await make(tmp_path, stagger=CHECK_STAGGER)
    try:
        (tmp_path / "clean.py").write_text("fine\n")
        d = await registry.get("clean")
        await registry.compute(d, FRAME, colour(1), warmup=0)  # a check waits for the pause
        runner.kill_for_tests()  # the worker dies; no job has noticed
        await until(lambda: not runner.up, "the worker to end")
        for _ in range(100):  # 25 s on the clock: well past the pause
            clock.advance(0.25)
            await asyncio.sleep(0.005)
        assert registry.checks_for(d, colour(1))["lookahead"] == "pending"
        assert not registry.check_log
        await registry.compute(d, FRAME, colour(2), warmup=0)  # a chart: a new worker
        restarted = runner.ready_at
        assert runner.up and restarted == clock.now
        await settled(registry, d, colour(1), clock, step=0.1)
        assert registry.check_log[0][1] >= restarted + CHECK_STAGGER
    finally:
        await registry.close()
        await runner.stop()


async def test_a_restarting_worker_holds_the_checks_too(tmp_path):
    registry, runner, clock = await make(tmp_path, timeout_s=2.0, stagger=CHECK_STAGGER)
    try:
        (tmp_path / "clean.py").write_text("fine\n")
        (tmp_path / "busy.py").write_text("HANG\n")
        d, busy = await registry.get("clean"), await registry.get("busy")
        await registry.compute(d, FRAME, colour(1), warmup=0)
        with pytest.raises(ScriptFailed):  # a timeout: the worker is replaced
            await registry.compute(busy, FRAME, colour(1))
        assert runner.state == "restarting"
        for _ in range(100):
            clock.advance(0.25)
            await asyncio.sleep(0.005)
        assert registry.checks_for(d, colour(1))["lookahead"] == "pending"
        await registry.compute(d, FRAME, colour(2), warmup=0)
        restarted = runner.ready_at
        await settled(registry, d, colour(1), clock, step=0.1)
        assert registry.check_log[0][1] >= restarted + CHECK_STAGGER
    finally:
        await registry.close()
        await runner.stop()
