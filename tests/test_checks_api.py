"""The save-time checks through the API and the real worker (CI-2 step c): where the
author sees them, that they run with the request's own parameters and `info`, and that
built-in indicators are not checked."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators.worker.runner import Limits
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import catalog, wait_for_worker, write
from tests.worker_helpers import real_launchers

HEAD = """
from openchart.indicators import Color, Float, Line, Marker, String, Width, ta

NAME = "T"
TITLE = "T"
OVERLAY = True
PARAMS = [Float("k", 1.0), String("tag", "a"), Color("color", "#2962ff"), Width("width", 1)]
OUTPUTS = [Line("x", "X", color="color", width="width")]

def warmup(params):
    return 20
"""

AHEAD = (
    HEAD
    + """
def compute(bars, params):
    return {"x": bars.close.shift(-1)}
"""
)

# It refuses to run unless it gets the request's parameters and instrument: a check that
# ran it with defaults or without `info` would fail instead of passing.
STRICT = (
    HEAD
    + """
def compute(bars, params, info):
    if info is None or info.instrument != "XAU_USD" or info.timeframe != "5m":
        raise ValueError(f"info {info!r}")
    if params["k"] != 3.5 or params["tag"] != "zz":
        raise ValueError(f"params {params!r}")
    return {"x": ta.sma(bars.close, 20) * params["k"]}
"""
)


@pytest.fixture(scope="module")
def wc(tmp_path_factory):
    harness = Harness(
        tmp_path_factory.mktemp("checks"),
        indicator_launchers=real_launchers(Limits(timeout_s=10)),
    )
    harness.seed_candles()
    (harness.config.data_dir / "indicators").mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.server.custom.check_stagger = c.server.custom.check_gap = 0.0
        c.folder = harness.config.data_dir / "indicators"
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def values(c, indicator, params=None, **query):
    q = {
        "instrument": "XAU_USD",
        "timeframe": "5m",
        "params": json.dumps(params or {}),
        "limit": 300,
        **query,
    }
    return c.get(f"/api/indicators/{indicator}", params=q)


def settled(c, indicator, params, seconds=30):
    end = time.monotonic() + seconds
    while True:
        got = values(c, indicator, params).json()["checks"]
        if "pending" not in (got["lookahead"], got["warmup"]):
            return got
        assert time.monotonic() < end, got
        time.sleep(0.1)


def test_a_lookahead_warning_in_the_values_and_the_catalog(wc):
    write(wc.folder, "ahead.py", AHEAD)
    first = values(wc, "ahead").json()
    assert first["checks"]["lookahead"] in ("pending", "warned")  # values are never held up
    assert len(first["values"]["x"]) == 300
    got = settled(wc, "ahead", {})
    assert got["lookahead"] == "warned" and got["warmup"] == "ok"
    (f,) = got["findings"]
    assert set(f) == {"check", "level", "output", "t", "full", "other", "message"}
    assert (f["check"], f["level"], f["output"]) == ("lookahead", "warning", "x")
    assert f["other"] is None and isinstance(f["full"], float)
    assert got["params"] == {"k": 1.0, "tag": "a", "color": "#2962ff", "width": 1}
    assert (got["instrument"], got["timeframe"], got["price"]) == ("XAU_USD", "5m", "mid")
    assert got["bars"] == 320 and got["declared_warmup"] == 20
    _, custom = catalog(wc)
    assert custom["ahead"]["checks"] == [got]
    assert custom["ahead"]["status"] == "ok"  # a warning, never a block


def test_the_checks_run_with_the_requests_parameters_and_info(wc):
    write(wc.folder, "strict.py", STRICT)
    params = {"k": 3.5, "tag": "zz"}
    assert values(wc, "strict", params).status_code == 200
    got = settled(wc, "strict", params)
    assert (got["lookahead"], got["warmup"], got["findings"]) == ("ok", "ok", [])


def test_builtin_indicators_are_not_checked(wc):
    body = values(wc, "ema", kind="builtin").json()
    assert "checks" not in body
    _, custom = catalog(wc)
    assert all(c["id"] != "ema" for c in custom.values())
