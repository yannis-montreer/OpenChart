"""CI-2 step (b) through the API and the real worker: the output kinds, String, `info`, the
long column names, and results that are checked before they are sent to the browser."""

import json

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators.worker.runner import Limits
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import GOOD, catalog, wait_for_worker, write
from tests.test_indicators import candles
from tests.worker_helpers import real_launchers


@pytest.fixture(scope="module")
def wc(tmp_path_factory):
    harness = Harness(
        tmp_path_factory.mktemp("kinds"),
        indicator_launchers=real_launchers(Limits(timeout_s=10)),
    )
    harness.seed_candles()
    (harness.config.data_dir / "indicators").mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.folder = harness.config.data_dir / "indicators"
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def values(c, indicator, params=None, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": json.dumps(params or {}), **query}
    return c.get(f"/api/indicators/{indicator}", params=q)


CROSS = '''
"""SMA cross: two averages, a band between them, levels, markers and a background."""
from openchart.indicators import (
    Area, Background, Color, Fill, Float, Histogram, Int, Level, Line, Marker, Source, String,
    Width, source, ta,
)

NAME = "SMA x"
TITLE = "SMA cross"
OVERLAY = True
PARAMS = [
    Int("fast", 2, min=1, max=50),
    Int("slow", 4, min=2, max=50),
    Float("limit", 1234.5, min=0),
    String("tag", "hi", max_length=4),
    Source(),
    Color("c1", "#2962ff"),
    Color("c2", "#ff6d00"),
    Width("w", 2),
]
OUTPUTS = [
    Line("fast", "Fast", color="c1", width="w", style="dashed"),
    Line("slow", "Slow", color="c2", width="w"),
    Histogram("hist", "Hist", color="c1", base=0),
    Area("area", "Area", color="c2"),
    Fill(between=("fast", "slow"), color="c1", opacity=0.2),
    Level(1000, "Fixed", color="c2"),
    Level("limit", "Limit", color="c1", width="w"),
    Marker("up", "Up", color="c1", shape="arrow_up", position="below"),
    Marker("down", "Down", color="c2", shape="arrow_down", position="above"),
    Background("bg", "Bg", color="c2", opacity=0.1),
]

def compute(bars, params):
    src = source(bars, params["source"])
    fast, slow = ta.sma(src, params["fast"]), ta.sma(src, params["slow"])
    return {
        "fast": fast,
        "slow": slow,
        "hist": fast - slow,
        "area": slow,
        "up": ta.crossover(fast, slow),
        "down": ta.crossunder(fast, slow),
        "bg": fast > slow,
    }
'''


def test_a_file_with_every_output_kind_describes_and_computes(wc):
    write(wc.folder, "cross.py", CROSS)
    _, custom = catalog(wc)
    assert custom["cross"]["status"] == "ok", custom["cross"]["reason"]
    definition = custom["cross"]["definition"]
    assert [(o["kind"], o["name"]) for o in definition["outputs"]] == [
        ("line", "fast"),
        ("line", "slow"),
        ("histogram", "hist"),
        ("area", "area"),
        ("fill", "fill:1"),
        ("level", "level:1"),
        ("level", "level:2"),
        ("marker", "up"),
        ("marker", "down"),
        ("background", "bg"),
    ]
    assert definition["outputs"][0]["style"] == "dashed"
    assert definition["outputs"][4]["between"] == ["fast", "slow"]
    assert next(p for p in definition["params"] if p["name"] == "tag") == {
        "name": "tag",
        "type": "string",
        "default": "hi",
        "label": "Tag",
        "group": "inputs",
        "max_length": 4,
    }

    got = values(wc, "cross", **{"from": 0, "limit": 400}).json()
    n = len(got["t"])
    assert n == 400
    assert set(got["values"]) == {"fast", "slow", "hist", "area"}
    assert all(len(v) == n for v in got["values"].values())
    assert set(got["hits"]) == {"up", "down", "bg"}
    for name, hits in got["hits"].items():
        assert hits == sorted(set(hits)), name  # ascending, no repeats
        assert all(isinstance(i, int) and 0 <= i < n for i in hits)
    assert got["levels"] == {"level:1": 1000.0, "level:2": 1234.5}

    closes = [b["c"] for b in candles(wc, **{"from": 0, "limit": 400})]
    fast = [None, *[(closes[i] + closes[i - 1]) / 2 for i in range(1, n)]]
    slow = [sum(closes[i - 3 : i + 1]) / 4 if i >= 3 else None for i in range(n)]
    assert got["values"]["slow"][3:] == pytest.approx(slow[3:])
    assert [i for i in got["hits"]["bg"] if i >= 3] == [i for i in range(3, n) if fast[i] > slow[i]]
    crossed = [i for i in range(4, n) if fast[i] > slow[i] and not fast[i - 1] > slow[i - 1]]
    assert [i for i in got["hits"]["up"] if i >= 4] == crossed


def test_a_levels_input_is_resolved_with_the_parameters_of_the_request(wc):
    write(wc.folder, "cross.py", CROSS)
    got = values(wc, "cross", params={"limit": 5}, **{"from": 0, "limit": 10}).json()
    assert got["levels"] == {"level:1": 1000.0, "level:2": 5.0}
    assert values(wc, "cross", params={"tag": "toolong"}).status_code == 400
    assert values(wc, "cross", params={"tag": "ok"}).status_code == 200


def test_hits_are_indexes_into_the_bars_asked_for_not_into_the_warmup(wc):
    write(wc.folder, "cross.py", CROSS)
    got = values(wc, "cross", **{"from": 0, "limit": 50}).json()
    assert len(got["t"]) == 50
    assert all(0 <= i < 50 for hits in got["hits"].values() for i in hits)


INFO_FILE = """
from openchart.indicators import Color, Line, Width

NAME = "Info"
OVERLAY = False
PARAMS = [Color("color", "#ff0000"), Width("width", 1)]
OUTPUTS = [
    Line("decimals", "Decimals", color="color", width="width"),
    Line("shape", "Shape", color="color", width="width"),
]

def compute(bars, params, info):
    assert (info.instrument, info.display_name, info.timeframe, info.price) == (
        "XAU_USD", "Gold", "5m", "mid"
    ), info
    return {"decimals": [info.decimals] * len(bars), "shape": bars.close - bars.c}
"""


def test_info_reaches_compute_when_it_declares_a_third_argument(wc):
    write(wc.folder, "whoami.py", INFO_FILE)
    got = values(wc, "whoami", **{"from": 0, "limit": 5}).json()
    assert got["values"]["decimals"] == [3.0] * 5
    assert got["values"]["shape"] == [0.0] * 5  # bars.close is bars.c
    for query in ({"price": "bid"}, {"timeframe": "1h"}):
        out = values(wc, "whoami", **query, **{"from": 0, "limit": 5})
        assert out.status_code == 422, out.text
        assert "AssertionError" in out.json()["detail"]


def test_a_two_argument_compute_is_not_given_info(wc):
    write(wc.folder, "plain.py", GOOD)
    assert values(wc, "plain", **{"from": 0, "limit": 5}).status_code == 200


HOSTILE = """
from openchart.indicators import Color, Line, Marker, Width

NAME = "Bad"
OVERLAY = False
PARAMS = [Color("color", "#ff0000"), Width("width", 1)]
OUTPUTS = [
    Line("v", "V", color="color", width="width"),
    Marker("m", "M", color="color"),
]

def compute(bars, params):
    n = len(bars)
    return BODY
"""


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("{'v': [1.0] * n}", "no 'm' output"),
        ("{'v': [1.0] * n, 'm': [True] * n, 'x': [1.0] * n}", "'x', which is not a declared"),
        ("{'v': [1.0] * (n + 1), 'm': [True] * n}", "'v' has"),
        ("{'v': [1.0] * 5_000_000, 'm': [True] * n}", "5,000,000 values"),
        ("{'v': ['x'] * n, 'm': [True] * n}", "'v' must be numbers"),
        ("{'v': [1.0] * n, 'm': [1.0] * n}", "'m' must be true/false"),
        ("{'v': [1.0] * n, 'm': [float('nan')] * n}", "'m' must be true/false"),
        ("[1.0] * n", "must return a dict"),
        ("{'v': 3, 'm': [True] * n}", "single value"),
    ],
)
def test_hostile_results_are_an_error_for_that_indicator_not_a_crash(wc, body, fragment):
    write(wc.folder, "hostile.py", HOSTILE.replace("BODY", body))
    out = values(wc, "hostile", **{"from": 0, "limit": 20})
    started = wc.server.indicator_runner.status()["pid"]
    assert out.status_code == 422, out.text
    assert out.json()["detail"].startswith("Bad failed: "), out.text
    assert fragment in out.json()["detail"], out.text
    assert catalog(wc)[1]["hostile"]["status"] == "ok"  # the file is fine; this run was not
    assert wc.get("/api/health").status_code == 200
    write(wc.folder, "mid.py", GOOD)
    assert values(wc, "mid").status_code == 200  # the worker still serves
    assert wc.server.indicator_runner.status()["pid"] == started  # and was not restarted
    (wc.folder / "hostile.py").unlink()


def test_a_line_of_only_gaps_comes_back_as_nulls(wc):
    body = "{'v': [float('nan')] * n, 'm': [False] * n}"
    write(wc.folder, "empty.py", HOSTILE.replace("BODY", body))
    got = values(wc, "empty", **{"from": 0, "limit": 12}).json()
    assert got["values"]["v"] == [None] * 12
    assert got["hits"] == {"m": []}
    (wc.folder / "empty.py").unlink()


def test_a_schema_error_names_the_file_the_output_and_the_rule(wc):
    write(wc.folder, "badref.py", GOOD.replace('color="color"', 'color="width"'))
    reason = catalog(wc)[1]["badref"]["reason"]
    assert reason.startswith("SchemaError: badref.py: output 'mid': 'width' is not a color")
    (wc.folder / "badref.py").unlink()


def test_a_file_over_256_kb_is_refused_without_running(wc, tmp_path):
    marker = tmp_path / "ran.txt"
    write(wc.folder, "huge.py", f"open({str(marker)!r}, 'w')\n" + GOOD + "#" * (256 * 1024))
    assert catalog(wc)[1]["huge"]["reason"] == "the file is larger than 256 KB"
    assert not marker.exists()
    (wc.folder / "huge.py").unlink()


def test_the_builtin_ema_over_the_api_has_the_same_values_and_the_new_fields(wc):
    got = values(wc, "ema", **{"from": 0, "limit": 30}).json()
    assert list(got["values"]) == ["ema"]
    assert got["hits"] == {} and got["levels"] == {}
    ema = wc.get("/api/indicators").json()["builtin"][0]
    assert ema["outputs"] == [
        {
            "kind": "line",
            "name": "ema",
            "label": "EMA",
            "color": "color",
            "width": "width",
            "style": "solid",
        }
    ]
