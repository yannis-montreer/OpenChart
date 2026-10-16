"""The save-time checks through the API (CI-2 step c): where the author sees them, that
they run with the request's own parameters and `info`, and that built-in indicators are
not checked.

With the fake worker (tests/fake_worker.py): what is tested here is what the server sends
and shows, not how a script runs, and the fake worker starts in a fraction of a second
however busy the machine is. Real scripts are checked in tests/test_lookahead_checks.py.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import catalog, wait_for_worker
from tests.worker_helpers import FakeLauncher

REAL_WAIT = 30.0


@pytest.fixture(scope="module")
def fc(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("checks"), indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder = harness.server, folder
        c.server.custom.check_stagger = c.server.custom.check_gap = 0.0
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


def settled(c, indicator, params):
    end = time.monotonic() + REAL_WAIT
    while True:
        got = values(c, indicator, params).json()["checks"]
        if "pending" not in (got["lookahead"], got["warmup"]):
            return got
        assert time.monotonic() < end, got
        time.sleep(0.05)


def test_a_lookahead_warning_in_the_values_and_the_catalog(fc):
    (fc.folder / "ahead.py").write_text("SHIFT WARMUP 20\n")
    first = values(fc, "ahead").json()
    assert first["checks"]["lookahead"] in ("pending", "warned")  # values are never held up
    assert len(first["values"]["out"]) == 300
    got = settled(fc, "ahead", {})
    assert got["lookahead"] == "warned" and got["warmup"] == "ok"
    (f,) = got["findings"]
    assert set(f) == {"check", "level", "output", "t", "full", "other", "message"}
    assert (f["check"], f["level"], f["output"]) == ("lookahead", "warning", "out")
    assert f["other"] is None and isinstance(f["full"], float)
    # 320 bars checked: the 300 asked for and the 20 of warmup before them.
    assert f["t"] == first["t"][320 - 50 - 1 - 20]
    assert got["params"] == {"color": "#2962ff", "width": 1}
    assert (got["instrument"], got["timeframe"], got["price"]) == ("XAU_USD", "5m", "mid")
    assert got["bars"] == 320 and got["declared_warmup"] == 20
    _, custom = catalog(fc)
    assert custom["ahead"]["checks"] == [got]
    assert custom["ahead"]["status"] == "ok"  # a warning, never a block


def test_the_checks_run_with_the_requests_parameters_and_info(fc):
    # The fake worker refuses any other parameters or instrument: a check run with the
    # defaults or without `info` would fail instead of passing.
    (fc.folder / "strict.py").write_text("STRICT\n")
    params = {"color": "#00ff00"}
    assert values(fc, "strict", params).status_code == 200
    assert values(fc, "strict").status_code == 422  # the refusal works
    got = settled(fc, "strict", params)
    assert (got["lookahead"], got["warmup"], got["findings"]) == ("ok", "ok", [])


def test_builtin_indicators_are_not_checked(fc):
    body = values(fc, "ema", kind="builtin").json()
    assert "checks" not in body
    _, custom = catalog(fc)
    assert all(c["id"] != "ema" for c in custom.values())
    assert not fc.server.custom._pending
