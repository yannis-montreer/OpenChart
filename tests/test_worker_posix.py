"""Linux and macOS: the sidecar (as `openchart indicator-worker --listen` runs it in its
container, here as a plain process) and the process-only worker: a clean environment,
hangs killed at the deadline while the sidecar carries on, memory limits."""

import asyncio
import hashlib
import os
import subprocess
import time

import numpy as np
import pytest

from openchart.indicators.worker import isolation
from openchart.indicators.worker.isolation import ProcessLauncher, SidecarLauncher, start_worker
from openchart.indicators.worker.runner import Limits, ScriptFailed

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux and macOS only")

SCRIPT = """
from openchart.indicators import Color, Output, Width, sma
NAME = "S"
OVERLAY = True
PARAMS = [Color("color", "#2962ff"), Width("width", 1)]
OUTPUTS = [Output("out", "Out", color="color", width="width")]
def compute(bars, params):
{body}
    return {{"out": sma(bars["c"], 2)}}
"""
BARS = {k: np.arange(10, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}


def script(body: str = "    pass"):
    text = SCRIPT.format(body=body)
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


@pytest.fixture
def sidecar(tmp_path):
    sock = str(tmp_path / "sock")
    argv, _ = isolation.worker_argv()
    argv += ["--listen", sock]
    # As in the image: OPENCHART_* in the container's environment, which the worker drops.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "OPENCHART_DATA_DIR": "/data",
        "OPENCHART_SECRET_KEY": "x",
    }
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    end = time.monotonic() + 60
    while not os.path.exists(sock):
        assert proc.poll() is None, proc.stdout.read()
        assert time.monotonic() < end
        time.sleep(0.05)
    yield sock
    proc.kill()
    proc.wait(10)


async def test_the_sidecar_cleans_its_environment_and_survives_hangs(sidecar, tmp_path):
    runner, iso = await start_worker(
        tmp_path, Limits(timeout_s=1, memory_mb=256), [SidecarLauncher(sidecar)]
    )
    assert runner is not None, iso.reason
    try:
        assert iso.probes["environment"]["result"] == "clean"  # OPENCHART_* removed
        assert iso.level == "sidecar"
        out = await runner.compute(*script(), {}, BARS)
        assert out["out"][1:3].tolist() == [0.5, 1.5]
        t = time.perf_counter()
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("    while True:\n        pass"), {}, BARS)
        assert failed.value.kind == "timeout" and time.perf_counter() - t < 2.5
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("    x = bytearray(2 * 2**30)"), {}, BARS)
        assert failed.value.kind == "memory"
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10  # same connection
        assert runner.restarts == 0
    finally:
        await runner.stop()


async def test_two_connections_at_once(sidecar, tmp_path):
    first, _ = await start_worker(tmp_path, Limits(timeout_s=2), [SidecarLauncher(sidecar)])
    second, _ = await start_worker(tmp_path, Limits(timeout_s=2), [SidecarLauncher(sidecar)])
    try:
        got = await asyncio.gather(
            first.compute(*script(), {}, BARS), second.compute(*script(), {}, BARS)
        )
        assert [g["out"].size for g in got] == [10, 10]
    finally:
        await first.stop()
        await second.stop()


async def test_the_process_only_worker_has_a_memory_limit_and_dies_with_its_deadline(tmp_path):
    runner, iso = await start_worker(
        tmp_path, Limits(timeout_s=1, memory_mb=256), [ProcessLauncher(Limits(memory_mb=256))]
    )
    assert runner is not None, iso.reason
    try:
        assert iso.level == "process-only" and iso.minimum_met is False
        assert iso.probes["environment"]["result"] == "clean"
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("    x = bytearray(2 * 2**30)"), {}, BARS)
        assert failed.value.kind == "memory"
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("    import time\n    time.sleep(60)"), {}, BARS)
        assert failed.value.kind == "timeout"
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10
    finally:
        await runner.stop()
