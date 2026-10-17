"""The findings of docs/security-review-custom-indicators.md, as tests.

S1, S2, S3 and S5 are fixed (hardening-1) and are plain tests. S6 is documented, not fixed:
its test is `xfail(strict=True)`, so it fails today and the suite turns red as soon as it is
fixed, when the marker and the review's "not fixed" note go. Findings fixed in CI-3's own
diff have their tests in test_chart_indicators.py.
"""

import hashlib
import time

import numpy as np
import pytest

from openchart.api.editing import decide
from openchart.indicators._base import Param
from openchart.indicators._custom import ID
from openchart.indicators.worker.isolation import Isolation
from openchart.indicators.worker.protocol import ProtocolError, parse_header
from openchart.indicators.worker.runner import IndicatorRunner, Limits, ScriptFailed
from tests.worker_helpers import FakeLauncher

ISOLATED = Isolation("appcontainer", True, "minimum isolation met")
DEEP = b"[" * 100_000 + b"]" * 100_000
BARS = {k: np.arange(5, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}


def test_s2_a_deeply_nested_header_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        parse_header(DEEP)


async def test_s2_a_worker_sending_a_deep_header_fails_the_job_at_once():
    runner = IndicatorRunner(FakeLauncher(), Limits(timeout_s=3))
    await runner.start()
    try:
        source = "DEEPHEADER"
        digest = hashlib.sha256(source.encode()).hexdigest()
        t = time.perf_counter()
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(digest, "deep.py", source, {}, BARS)
        # A bad frame should end the job as one, at once, not as a timeout that counts
        # toward the definition's quarantine.
        assert failed.value.kind != "timeout"
        assert time.perf_counter() - t < 1.5
    finally:
        await runner.stop()


def test_s3_infinity_in_the_values_params_is_a_400(client):
    r = client.get(
        "/api/indicators/ema",
        params={"instrument": "XAU_USD", "timeframe": "1h", "params": '{"length": Infinity}'},
    )
    assert r.status_code == 400


def test_s3_a_huge_number_for_a_float_input_is_refused_with_a_message():
    with pytest.raises(ValueError):
        Param("x", "float", 1.0).check(10**400)


WRITES_A_FRAME = """
import sys
import numpy as np
from openchart.indicators import Color, Output, Width
from openchart.indicators.worker.protocol import encode

NAME = "A"
OVERLAY = True
PARAMS = [Color("color", "#ff0000"), Width("width", 1)]
OUTPUTS = [Output("out", "Out", color="color", width="width")]

def compute(bars, params):
    # Only sys.stdout points at stderr; the protocol's own stream is still reachable.
    sys.__stdout__.buffer.write(encode({"outputs": ["out"]}, {"out": np.zeros(len(bars))}))
    sys.__stdout__.buffer.flush()
    return {"out": bars["close"]}
"""
DOUBLES = WRITES_A_FRAME.replace('NAME = "A"', 'NAME = "B"').split("def compute")[0] + (
    'def compute(bars, params):\n    return {"out": bars["close"] * 2}\n'
)


async def test_s6_a_script_cannot_answer_for_the_next_request(tmp_path):
    from openchart.indicators.worker.isolation import start_worker
    from tests.worker_helpers import real_launchers

    limits = Limits(timeout_s=10)
    runner, _ = await start_worker(tmp_path, limits, launchers=real_launchers(limits))
    assert runner is not None
    bars = {k: np.arange(1, 6, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}
    try:
        a = (hashlib.sha256(WRITES_A_FRAME.encode()).hexdigest(), "a.py", WRITES_A_FRAME)
        b = (hashlib.sha256(DOUBLES.encode()).hexdigest(), "b.py", DOUBLES)
        # A's forged frame has no request id: A's own job fails loudly, the worker goes.
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*a, {}, bars)
        assert failed.value.kind == "protocol" and "another request" in str(failed.value)
        got = await runner.compute(*b, {}, bars)
        assert got["out"].tolist() == [2, 4, 6, 8, 10]  # B's own answer
    finally:
        await runner.stop()


def test_s5_a_custom_id_with_a_trailing_newline_is_not_an_id():
    assert not ID.match("mine\n")


@pytest.mark.parametrize("host", ["myhost.tail1234.ts.net", "203.0.113.7:8000", "evil.test"])
def test_s1_auto_refuses_a_loopback_peer_that_names_another_host(host):
    """A local forwarder that adds no header (an SSH tunnel, socat, a tunnel agent; to
    check: Tailscale Funnel) makes a remote browser's request arrive from 127.0.0.1. The
    Host it asked for still names the remote address."""
    editing = decide(
        setting="auto",
        behind_proxy=False,
        isolation=ISOLATED,
        peer="127.0.0.1",
        headers={"host": host},
    )
    assert not editing.allowed
