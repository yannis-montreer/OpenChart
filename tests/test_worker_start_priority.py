"""The worker starts at normal priority and the server lowers it to below normal right after
its hello message (docs/investigations/worker-start-under-load.md): a below-normal worker
starves when every core is busy, so it never got to say hello.

What is held here, with a fake worker that records what the server does with it:

- the priority is lowered after the hello and before the first request, and read back;
- no script source is sent before the hello (the worker is at normal priority until then);
- fail closed: a priority that cannot be set, or does not read back as below normal, kills
  the worker and the indicators report why; no worker is left at normal priority;
- a respawn (after a kill or a deadline) takes the same path;
- a start that fails says how much CPU the worker had used (starved or hung);
- the start deadline is 90 s.
"""

import asyncio
import hashlib
import logging

import numpy as np
import pytest

from openchart.indicators.worker import runner as runner_module
from openchart.indicators.worker.isolation import start_worker
from openchart.indicators.worker.runner import IndicatorRunner, Limits, WorkerUnavailable
from tests.worker_helpers import FakeLauncher, PopenHandle

BARS = {k: np.arange(5, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}
SECRET_SOURCE = "SECRET_SOURCE_MARKER"


def script(text: str):
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


class Tap:
    """A pipe that notes what passes through it, in order, in a list shared by a launcher."""

    def __init__(self, f, events, kind):
        self._f, self._events, self._kind, self._seen = f, events, kind, False

    def read(self, n):
        data = self._f.read(n)
        if data and not self._seen:
            self._seen = True
            self._events.append(("hello bytes",))
        return data

    def write(self, data):
        self._events.append(("write", bytes(data)))
        return self._f.write(data)

    def __getattr__(self, name):
        return getattr(self._f, name)


class LoweringHandle(PopenHandle):
    """A fake worker's handle that can lower its priority, as `windows.WinProcess` does."""

    def __init__(self, popen, launcher):
        super().__init__(popen)
        self.launcher = launcher
        self.events = launcher.events
        self.stdin = Tap(self.stdin, self.events, "in")
        self.stdout = Tap(self.stdout, self.events, "out")
        self.current = "normal"

    def lower_priority(self):
        self.events.append(("lower", self.pid))
        if self.launcher.fail_lowering(self):
            raise OSError(5, "SetPriorityClass: Access is denied.")
        if not self.launcher.ineffective:
            self.current = "below_normal"

    def priority(self):
        self.events.append(("priority", self.pid))
        return None if self.launcher.unreadable else self.current

    def cpu_seconds(self):
        return self.launcher.cpu


class LoweringLauncher(FakeLauncher):
    """The Windows launcher's contract: the worker starts at normal priority and the server
    lowers it."""

    lowers_priority_after_hello = True

    def __init__(self, *, fail_from=None, ineffective=False, unreadable=False, cpu=0.0):
        super().__init__()
        self.events: list[tuple] = []
        self.handles: list[LoweringHandle] = []
        self.fail_from = fail_from  # the nth worker (1-based) whose lowering raises
        self.ineffective = ineffective  # lowering "succeeds" but changes nothing
        self.unreadable = unreadable  # the read-back gives nothing
        self.cpu = cpu

    def start(self):
        handle = super().start()
        wrapped = LoweringHandle(handle.popen, self)
        self.handles.append(wrapped)
        return wrapped

    def fail_lowering(self, handle):
        return self.fail_from is not None and len(self.handles) >= self.fail_from


def names(events):
    return [e[0] for e in events]


async def test_the_priority_is_lowered_after_hello_and_before_the_first_request():
    launcher = LoweringLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    await runner.start()
    try:
        order = names(launcher.events)
        # hello, then lowered and read back, then the first request (the probes' absence
        # here: this start has none, so it is the arm)
        assert order[:4] == ["hello bytes", "lower", "priority", "write"]
        assert launcher.handles[0].current == "below_normal"
        await runner.compute(*script("fine"), {}, BARS)
        assert order.count("lower") == 1
    finally:
        await runner.stop()


async def test_no_user_source_reaches_the_worker_before_its_hello(monkeypatch):
    monkeypatch.setenv("FAKE_HELLO_DELAY", "0.8")
    launcher = LoweringLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=2.0))
    starting = asyncio.create_task(runner.start())
    await asyncio.sleep(0.3)  # the worker is still starting: no hello yet
    job = asyncio.create_task(runner.compute(*script(SECRET_SOURCE), {}, BARS))
    await asyncio.sleep(0.2)
    assert not [e for e in launcher.events if e[0] == "write"], "bytes sent before the hello"
    try:
        await starting
        await job
        order = names(launcher.events)
        first_source = next(
            i
            for i, e in enumerate(launcher.events)
            if e[0] == "write" and SECRET_SOURCE.encode() in e[1]
        )
        assert order.index("hello bytes") < order.index("lower") < first_source
        assert order.index("priority") < first_source
    finally:
        await runner.stop()


@pytest.mark.parametrize(
    ("launcher_args", "reason"),
    [
        ({"fail_from": 1}, "Access is denied"),
        ({"ineffective": True}, "normal"),
        ({"unreadable": True}, "priority"),
    ],
)
async def test_a_priority_that_is_not_below_normal_kills_the_worker(launcher_args, reason):
    launcher = LoweringLauncher(**launcher_args)
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    with pytest.raises(WorkerUnavailable) as failed:
        await runner.start()
    assert "priority" in str(failed.value) and reason in str(failed.value)
    assert runner.state == "failed" and runner.last_error == str(failed.value)
    assert not launcher.handles[0].alive()  # never left running at normal priority
    assert not [e for e in launcher.events if e[0] == "write"]  # nothing was sent to it


async def test_a_launcher_that_cannot_lower_the_priority_runs_no_worker():
    class NoLowering(FakeLauncher):
        lowers_priority_after_hello = True  # promises it, but its handles have no way

    launcher = NoLowering()
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    with pytest.raises(WorkerUnavailable, match="priority"):
        await runner.start()


async def test_the_custom_section_says_why_when_the_priority_fails(tmp_path):
    from openchart.indicators._custom import CustomIndicators

    launcher = LoweringLauncher(fail_from=1)
    runner, iso = await start_worker(tmp_path, Limits(timeout_s=1.0), [launcher])
    assert runner is None and iso.level == "unavailable" and not iso.runs_custom_code
    assert "priority" in iso.notice and "Access is denied" in iso.notice
    folder = tmp_path / "indicators"
    folder.mkdir()
    (folder / "mine.py").write_text("NAME = 'Mine'\n")
    registry = CustomIndicators(folder)
    registry.worker_ready(None, iso)
    d = await registry.get("mine")
    assert d.status == "not_run" and d.reason == iso.notice
    assert not launcher.handles[0].alive()


async def test_a_respawn_after_a_kill_or_a_deadline_lowers_the_priority_again():
    launcher = LoweringLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    await runner.start()
    try:
        runner.kill_for_tests()
        launcher.handles[0].wait(5000)
        await runner.compute(*script("after the kill"), {}, BARS)
        with pytest.raises(Exception, match="timed out"):
            await runner.compute(*script("HANG"), {}, BARS)
        await runner.compute(*script("after the deadline"), {}, BARS)
        assert len(launcher.handles) == 3
        assert names(launcher.events).count("lower") == 3
        assert names(launcher.events).count("priority") == 3
        assert [h.current for h in launcher.handles[1:]] == ["below_normal"] * 2
        # in every worker, the first request came after its lowering
        for h in launcher.handles:
            assert [e for e in launcher.events if e[0] == "lower" and e[1] == h.pid]
    finally:
        await runner.stop()


async def test_a_respawn_whose_lowering_fails_kills_that_worker_and_reports_why():
    launcher = LoweringLauncher(fail_from=2)
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    await runner.start()
    try:
        runner.kill_for_tests()
        launcher.handles[0].wait(5000)
        with pytest.raises(WorkerUnavailable, match=r"priority.*Access is denied"):
            await runner.compute(*script("after the kill"), {}, BARS)
        assert not launcher.handles[1].alive()
        assert not runner.up
    finally:
        await runner.stop()


async def test_a_failed_start_says_how_much_cpu_the_worker_had_used(monkeypatch, caplog):
    monkeypatch.setenv("FAKE_HELLO_DELAY", "5")
    monkeypatch.setattr(runner_module, "START_TIMEOUT", 0.5)
    launcher = LoweringLauncher(cpu=0.42)
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    with (
        caplog.at_level(logging.WARNING, logger="openchart.indicators.worker"),
        pytest.raises(WorkerUnavailable) as failed,
    ):
        await runner.start()
    assert "did not start within 0.5 s" in str(failed.value)
    assert "used 0.42 s of CPU" in str(failed.value)
    assert any("used 0.42 s of CPU" in r.getMessage() for r in caplog.records)
    assert not launcher.handles[0].alive()


async def test_a_launcher_without_the_contract_is_left_as_it_was():
    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=1.0))
    await runner.start()
    await runner.stop()


def test_the_start_deadline_is_90_seconds():
    assert runner_module.START_TIMEOUT == 90.0
