"""A warmup the values route cannot fetch is said, not hidden.

The route fetches at most MAX_WARMUP (20,000) bars before the first one shown. An indicator
that needs more (a built-in EMA above length 1999, a custom file that declares a large
warmup) used to get its values computed on too little history with nothing to say so. Now
the answer carries a `notice` (and, for a custom file, the same in `checks`).
"""

import json
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.api.routes_indicators import MAX_WARMUP
from openchart.indicators.worker.runner import Limits
from tests.conftest import Harness, run_setup
from tests.worker_helpers import real_launchers


def values(c, indicator, params=None, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": json.dumps(params or {}), **query}
    return c.get(f"/api/indicators/{indicator}", params=q)


# Built-in ---------------------------------------------------------------------------------


def test_an_ema_that_fits_has_no_notice(client):
    # warmup 10 * 1999 + 10 = 20,000: exactly the cap, nothing is cut
    body = values(client, "ema", {"length": 1999}, limit=20).json()
    assert "notice" not in body


def test_an_ema_above_length_1999_says_its_warmup_was_cut(client):
    body = values(client, "ema", {"length": 2000}, limit=20).json()
    notice = body["notice"]
    assert notice["kind"] == "warmup_cut"
    assert notice["needed"] == 10 * 2000 + 10
    assert notice["limit"] == MAX_WARMUP
    assert "20,010" in notice["message"] and "20,000" in notice["message"]
    assert "may differ" in notice["message"]
    assert "checks" not in body  # built-in: no save-time checks


def test_a_small_indicator_never_has_one(client):
    for oid, params in [
        ("ema", {}),
        ("rsi", {"length": 900}),
        ("macd", {"slow": 900, "signal": 900}),
    ]:
        assert "notice" not in values(client, oid, params, limit=20).json(), oid


def test_the_values_are_still_returned_with_the_notice(client):
    body = values(client, "ema", {"length": 2000}, limit=20).json()
    assert len(body["t"]) == 20 and "ema" in body["values"]


# Custom -----------------------------------------------------------------------------------

BIG = """
from openchart.indicators import Color, Output, Width, sma, source

NAME = "Big"
TITLE = "Big warmup"
OVERLAY = False
PARAMS = [Color("color", "#ff0000"), Width("width", 1)]
OUTPUTS = [Output("x", "X", color="color", width="width")]

def warmup(p):
    return {warmup}

def compute(bars, params):
    return {{"x": sma(source(bars, "close"), 3)}}
"""


@pytest.fixture(scope="module")
def wc(tmp_path_factory):
    harness = Harness(
        tmp_path_factory.mktemp("notice"),
        indicator_launchers=real_launchers(Limits(timeout_s=10)),
    )
    harness.seed_candles()
    (harness.config.data_dir / "indicators").mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.folder = harness.config.data_dir / "indicators"
        assert run_setup(c).status_code == 200
        end = time.monotonic() + 60
        while c.server.isolation is None:
            assert time.monotonic() < end, "the worker did not start"
            time.sleep(0.1)
        yield c


def put(wc, name, warmup):
    (wc.folder / f"{name}.py").write_text(
        textwrap.dedent(BIG.format(warmup=warmup)), encoding="utf-8"
    )


def test_a_custom_file_with_a_large_warmup_says_so_in_notice_and_checks(wc):
    put(wc, "big", 30_000)
    body = values(wc, "big", kind="custom", limit=20).json()
    notice = body["notice"]
    assert notice["kind"] == "warmup_cut" and notice["needed"] == 30_000
    assert notice["limit"] == MAX_WARMUP
    cut = body["checks"]["warmup_cut"]
    assert cut["needed"] == 30_000 and cut["limit"] == MAX_WARMUP and cut["message"]
    assert len(body["t"]) == 20


def test_a_custom_file_within_the_cap_has_neither(wc):
    put(wc, "small", MAX_WARMUP)
    body = values(wc, "small", kind="custom", limit=20).json()
    assert "notice" not in body
    assert "warmup_cut" not in (body.get("checks") or {})


def test_the_stored_checks_do_not_keep_the_notice(wc):
    put(wc, "swing", 30_000)
    assert "warmup_cut" in values(wc, "swing", kind="custom", limit=20).json()["checks"]
    # The request's copy carries it; what the server keeps for the file's version does not.
    assert "warmup_cut" not in json.dumps(wc.get("/api/indicators").json())
    again = values(wc, "swing", kind="custom", limit=20).json()
    assert again["checks"]["warmup_cut"]["needed"] == 30_000
