"""CI-5 (design 3.8): computed values are cached in the server, style-only changes never
recompute, identical requests share one computation, the forming bar is recomputed at
most once a second per chart query, and the server never holds an oversized answer from
the worker (S4).

With the fake worker (tests/fake_worker.py) and the built-in EMA; the timings against the
design's targets are measured by tools/measure_values.py with the real worker.
"""

import dataclasses
import io
import threading
import time
import tracemalloc

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators.worker.protocol import encode, exact_reader, read_frame
from openchart.providers.base import Tick
from tests.conftest import NOW, Harness, run_setup
from tests.test_custom_indicators import wait_for_worker, write
from tests.worker_helpers import FakeLauncher

LATER = 10**9


@pytest.fixture
def vc(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder, c.root = harness.server, folder, tmp_path
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        c.computes = count_computes(c)
        yield c


def count_computes(c):
    """How many compute jobs reached the worker."""
    calls = []
    runner = c.server.custom.runner
    real = runner.compute

    async def counted(*args, **kwargs):
        calls.append(args[3] if len(args) > 3 else kwargs.get("params"))
        return await real(*args, **kwargs)

    runner.compute = counted
    return calls


def values(c, indicator, params="{}", **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": params, "limit": 300, **query}
    r = c.get(f"/api/indicators/{indicator}", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def without_checks(body):
    return {k: v for k, v in body.items() if k != "checks"}


# The cache ----------------------------------------------------------------------------------


def test_the_same_request_is_answered_from_the_cache(vc):
    write(vc.folder, "mine.py", "PLAIN\n")
    first = values(vc, "mine")
    second = values(vc, "mine")
    assert len(vc.computes) == 1
    assert without_checks(second) == without_checks(first)


def test_a_style_only_change_never_recomputes(vc):
    write(vc.folder, "mine.py", "PLAIN\n")
    first = values(vc, "mine")
    restyled = values(vc, "mine", params='{"color": "#00ff00", "width": 3}')
    assert len(vc.computes) == 1
    assert restyled["params"] == {"color": "#00ff00", "width": 3}  # the answer's own
    assert restyled["values"] == first["values"]


def test_an_input_change_recomputes(vc):
    write(vc.folder, "mode.py", "MODE\n")
    values(vc, "mode", params='{"mode": "fast"}')
    values(vc, "mode", params='{"mode": "slow"}')
    values(vc, "mode", params='{"mode": "fast", "color": "#00ff00"}')  # style: cached
    assert len(vc.computes) == 2


def test_a_code_change_recomputes(vc):
    write(vc.folder, "mine.py", "PLAIN\n")
    values(vc, "mine")
    write(vc.folder, "mine.py", "PLAIN edited\n", later_ns=LATER)
    values(vc, "mine")
    assert len(vc.computes) == 2


def test_rewritten_bars_recompute(vc):
    """A repair or refill rewrites stored bars: the input differs, so does the key."""
    write(vc.folder, "mine.py", "PLAIN\n")
    first = values(vc, "mine")
    from tests.conftest import CANDLES

    t = first["t"][-10]
    changed = [
        dataclasses.replace(c, mid=tuple(x + 1 for x in c.mid), bid=tuple(x + 1 for x in c.bid))
        for c in CANDLES
        if t <= c.ts < t + 300
    ]
    vc.portal.call(vc.server.store.upsert_m1, vc.server.source, "XAU_USD", changed)
    second = values(vc, "mine")
    assert len(vc.computes) == 2
    assert second["values"]["out"][-10] != first["values"]["out"][-10]


def test_the_cache_is_bounded_least_recently_used_first(vc):
    write(vc.folder, "mine.py", "PLAIN\n")
    cache = vc.server.values_cache
    cache.max_bytes = 3 * 300 * 8 + 100  # room for three answers of 300 values
    for n in (300, 299, 298, 297):
        values(vc, "mine", limit=n)
    assert cache.nbytes <= cache.max_bytes and len(cache) == 3
    values(vc, "mine", limit=297)  # still there
    assert len(vc.computes) == 4
    values(vc, "mine", limit=300)  # the oldest went first
    assert len(vc.computes) == 5


def test_builtins_are_cached_too(vc, monkeypatch):
    ema = vc.server.indicators.get("ema")
    runs = []
    real = ema.run

    def counted(*args, **kwargs):
        runs.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ema, "run", counted)
    values(vc, "ema", params='{"length": 21}')
    values(vc, "ema", params='{"length": 21, "color": "#00ff00"}')
    values(vc, "ema", params='{"length": 22}')
    assert len(runs) == 2


def test_failures_are_not_cached_as_values(vc):
    write(vc.folder, "bad.py", "RAISE\n")
    q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 300}
    assert vc.get("/api/indicators/bad", params=q).status_code == 422
    write(vc.folder, "bad.py", "PLAIN now\n", later_ns=LATER)
    assert vc.get("/api/indicators/bad", params=q).status_code == 200


# One computation for every tab ----------------------------------------------------------------


def test_identical_requests_at_once_share_one_computation(vc, tmp_path):
    gate = tmp_path / "gate"
    gate.mkdir()
    write(vc.folder, "slow.py", f"GATE {gate}\n")
    out = [None] * 4
    threads = [
        threading.Thread(target=lambda i=i: out.__setitem__(i, values(vc, "slow")))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    end = time.monotonic() + 10
    while not list(gate.glob("started-*")):
        assert time.monotonic() < end
        time.sleep(0.01)
    time.sleep(0.3)  # the other three are waiting by now
    (gate / "go").write_text("")
    for t in threads:
        t.join(20)
    assert len(list(gate.glob("started-*"))) == 1 and len(vc.computes) == 1
    assert all(without_checks(o) == without_checks(out[0]) for o in out)


# The forming bar ------------------------------------------------------------------------------


def tick(c, t, price):
    c.portal.call(c.server.feed.handle_tick, Tick("XAU_USD", t, price - 0.1, price + 0.1))


def test_the_forming_bar_is_recomputed_at_most_once_a_second(vc):
    write(vc.folder, "mine.py", "PLAIN\n")
    clock = [1000.0]
    vc.server.values_cache.clock = lambda: clock[0]
    tick(vc, NOW + 30, 2000.0)
    first = values(vc, "mine", limit=2)
    tick(vc, NOW + 31, 2001.0)  # the forming bar moved
    again = values(vc, "mine", limit=2)
    assert len(vc.computes) == 1 and again["values"] == first["values"]
    clock[0] += 1.0
    later = values(vc, "mine", limit=2)
    assert len(vc.computes) == 2 and later["values"]["out"][-1] == pytest.approx(2001.0)


async def test_a_new_bar_is_never_held_back_by_the_second():
    """A window is its first and last bar: the next bar is another window, computed at once
    even within the second; the same window is not (on the cache itself: the feed's own
    timing decides when a stored bar appears)."""
    from openchart.indicators._cache import ValueCache

    cache, calls = ValueCache(clock=lambda: 1000.0), []

    def computing(value):
        async def compute():
            calls.append(value)
            return {"out": pd.Series([value])}

        return compute

    a = await cache.get(("v1", "bars A"), computing(1), window=("v1", 0, 300))
    b = await cache.get(("v1", "bars A'"), computing(2), window=("v1", 0, 300))  # a tick
    c = await cache.get(("v1", "bars B"), computing(3), window=("v1", 0, 600))  # a new bar
    assert calls == [1, 3]
    assert b["out"].tolist() == a["out"].tolist() == [1] and c["out"].tolist() == [3]
    d = await cache.get(("v1", "bars B"), computing(4))  # complete: by its exact key
    assert d["out"].tolist() == [3] and calls == [1, 3]


# S4: the server never holds an oversized answer ---------------------------------------------


def frame_with(count):
    return encode({"outputs": ["out"]}, {"out": np.zeros(count)})


def test_an_oversized_array_is_skipped_not_held():
    data = frame_with(3_000_000)  # 24 MB of values for a 20-bar job
    read = exact_reader(io.BytesIO(data))
    del data
    tracemalloc.start()
    try:
        _head, arrays = read_frame(read, max_count=20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 2**20  # a few MB of reading, not 24 (or 48 with the copy)
    a = arrays["out"]
    assert len(a) == 3_000_000 and a.dtype == np.float64 and a.ndim == 1
    assert a.strides == (0,)  # a stand-in that holds no values: the check names its length


def test_arrays_within_the_limit_are_read_as_before():
    _head, arrays = read_frame(exact_reader(io.BytesIO(frame_with(20))), max_count=20)
    assert arrays["out"].tolist() == [0.0] * 20
    _head, arrays = read_frame(exact_reader(io.BytesIO(frame_with(25))))  # no limit given
    assert len(arrays["out"]) == 25


def test_a_frame_cut_short_inside_a_skipped_array_is_the_end(tmp_path):
    data = frame_with(1000)[:-100]
    with pytest.raises(EOFError):
        read_frame(exact_reader(io.BytesIO(data)), max_count=10)


def test_the_runner_reads_with_the_jobs_bar_count(vc):
    """Through the API: the oversized answer is refused with the usual message and the
    worker keeps running (tests/test_untrusted_results.py pins both)."""
    write(vc.folder, "liar.py", "MARKERS BAD huge\n")
    tracemalloc.start()
    try:
        r = vc.get(
            "/api/indicators/liar", params={"instrument": "XAU_USD", "timeframe": "5m", "limit": 20}
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.status_code == 422 and "3,000,000 values for 20 bars" in r.json()["detail"]
    assert peak < 16 * 2**20
