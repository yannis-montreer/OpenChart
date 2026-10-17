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


def test_the_sidecars_own_answers_echo_the_request_id():
    """Found by CI on Linux: the sidecar answers arming, a source that does not match its
    hash, a timeout and a crash itself, not through `Worker.handle`, and those answers
    carried no id, so the runner refused them (the sidecar never armed)."""
    import socket
    import threading

    from openchart.indicators.worker import posix
    from openchart.indicators.worker.protocol import encode, exact_reader, read_frame

    class Stub:
        def hello(self):
            return {"ready": True}

        def learn(self, req):
            raise ValueError("the source does not match its hash")

    server_end, client_end = socket.socketpair()
    thread = threading.Thread(target=posix._serve, args=(server_end, Stub()), daemon=True)
    thread.start()
    f = client_end.makefile("rwb", buffering=0)
    read = exact_reader(f)
    assert read_frame(read)[0] == {"ready": True}
    f.write(encode({"cmd": "arm", "memory_mb": 256, "rid": 7}))
    assert read_frame(read)[0] == {"armed": True, "rid": 7}
    f.write(encode({"cmd": "compute", "hash": "x", "source": "y", "rid": 8}))
    head, _ = read_frame(read)
    assert head["rid"] == 8 and head["error"]["type"] == "ValueError"
    f.write(encode({"cmd": "exit"}))
    thread.join(5)
    client_end.close()
