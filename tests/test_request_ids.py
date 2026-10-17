"""Request ids on the worker protocol (S6, a CI-4 constraint): every request carries an id
the answer must echo, so an answer read for the wrong request (a frame a script wrote on
the protocol stream, a desynchronised worker) fails loudly and the worker is replaced,
instead of being taken as another request's answer."""

import hashlib

import numpy as np
import pytest

from openchart.indicators.worker.child import Worker
from openchart.indicators.worker.runner import IndicatorRunner, Limits, ScriptFailed
from tests.worker_helpers import FakeLauncher

BARS = {k: np.arange(5, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}


def script(text):
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


async def test_an_answer_with_another_request_id_is_refused_and_the_worker_replaced():
    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=3))
    await runner.start()
    try:
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("WRONGRID"), {}, BARS)
        assert failed.value.kind == "protocol"
        assert "another request" in str(failed.value)
        out = await runner.compute(*script("fine"), {}, BARS)
        assert out["out"].tolist() == [0, 1, 2, 3, 4]
        assert launcher.started == 2
    finally:
        await runner.stop()


async def test_every_request_gets_its_own_id(monkeypatch):
    from openchart.indicators.worker import runner as runner_module

    launcher = FakeLauncher()
    runner = IndicatorRunner(launcher, Limits(timeout_s=3))
    await runner.start()
    sent = []
    real = runner_module.encode

    def spy(header, arrays=None):
        if header.get("cmd") == "compute":
            sent.append(header.get("rid"))
        return real(header, arrays)

    monkeypatch.setattr(runner_module, "encode", spy)
    try:
        for text in ("a", "b", "a"):
            await runner.compute(*script(text), {}, BARS)
        assert len(sent) == 3 and len(set(sent)) == 3 and all(isinstance(r, int) for r in sent)
    finally:
        await runner.stop()


def test_the_real_worker_echoes_the_request_id():
    worker = Worker()
    head, _ = worker.handle({"cmd": "nope", "rid": 41}, {})
    assert head["rid"] == 41
    head, _ = worker.handle({"cmd": "describe", "hash": "x", "rid": 42}, {})
    assert head["rid"] == 42  # also on an error answer
