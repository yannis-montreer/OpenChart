"""The examples in docs/custom-indicators.md are real files.

Each one in `docs/examples/indicators/` runs in the real worker on fake bars (a seeded random
walk, so patterns such as an engulfing candle and bands crossings occur), draws something, gets
`ok` from both save-time checks with no findings, and gives the same value on a bar whichever
later bars exist (no lookahead). The page shows each file verbatim; the test compares them, so the
page cannot drift from what runs. The Pine cheat sheet may only name `ta` helpers that exist."""

import asyncio
import json
import math
import random
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators import ta
from openchart.indicators._base import OUTPUT_TYPES
from openchart.indicators.worker.runner import Limits
from openchart.market.instruments import METAL_HOURS
from openchart.providers.base import OHLC, Candle
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from tests.conftest import GOLD, NOW, Harness, run_setup
from tests.test_custom_indicators import catalog, wait_for_worker
from tests.worker_helpers import real_launchers

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "examples" / "indicators"
PAGE = ROOT / "docs" / "custom-indicators.md"
NAMES = sorted(p.stem for p in EXAMPLES.glob("*.py"))
SPREAD = 0.2


def walk(minutes: int = 3 * 1440, seed: int = 11) -> list[Candle]:
    """Noisy M1 candles that end at NOW: patterns occur, and the same every run."""
    rnd = random.Random(seed)
    price, out = 2000.0, []
    for ts in range(NOW - minutes * 60, NOW, 60):
        o = price
        price = o + (2000 + 8 * math.sin(ts / 3600) - o) * 0.02 + rnd.gauss(0, 0.5)
        c = price
        h, low = max(o, c) + abs(rnd.gauss(0, 0.25)), min(o, c) - abs(rnd.gauss(0, 0.25))
        bid = OHLC(*(round(x - SPREAD / 2, 3) for x in (o, h, low, c)))
        ask = OHLC(*(round(x + SPREAD / 2, 3) for x in (o, h, low, c)))
        mid = OHLC(*(round((a + b) / 2, 4) for a, b in zip(bid, ask, strict=True)))
        out.append(Candle(ts, bid, ask, mid, volume=10))
    return out


@pytest.fixture(scope="module")
def ex(tmp_path_factory):
    harness = Harness(
        tmp_path_factory.mktemp("docs"), indicator_launchers=real_launchers(Limits(timeout_s=10))
    )

    async def seed():
        async with await Database.open(harness.config.db_path) as db:
            store = CandleStore(db.conn)
            await store.upsert_instrument("fake", GOLD, METAL_HOURS)
            await store.upsert_m1("fake", "XAU_USD", walk())

    asyncio.run(seed())
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    for name in NAMES:
        (folder / f"{name}.py").write_text((EXAMPLES / f"{name}.py").read_text(encoding="utf-8"))
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        c.server.custom.check_stagger = c.server.custom.check_gap = 0.0
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def values(c, name, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": "{}", "limit": 600, **query}
    return c.get(f"/api/indicators/{name}", params=q)


def test_there_are_four_examples():
    assert NAMES == ["bollinger_fill", "engulfing", "rsi_bands", "sma_cross"]


@pytest.mark.parametrize("name", NAMES)
def test_an_example_loads_and_draws_something(ex, name):
    _, custom = catalog(ex)
    assert custom[name]["status"] == "ok", custom[name].get("reason")
    body = values(ex, name).json()
    n = len(body["t"])
    assert n == 600
    outputs = custom[name]["definition"]["outputs"]
    for o in outputs:
        if o["kind"] in ("line", "histogram", "area"):
            got = [v for v in body["values"][o["name"]] if v is not None]
            assert got and all(math.isfinite(v) for v in got), o["name"]
        if o["kind"] in ("marker", "background"):
            assert body["hits"][o["name"]], f"{o['name']} never fires on the fake bars"
            assert len(body["hits"][o["name"]]) < n, f"{o['name']} fires on every bar"
        if o["kind"] == "level":
            assert math.isfinite(body["levels"][o["name"]])


@pytest.mark.parametrize("name", NAMES)
def test_an_example_passes_both_save_time_checks(ex, name):
    end = time.monotonic() + 30
    while True:
        checks = values(ex, name).json()["checks"]
        if "pending" not in (checks["lookahead"], checks["warmup"]):
            break
        assert time.monotonic() < end, checks
        time.sleep(0.05)
    assert (checks["lookahead"], checks["warmup"]) == ("ok", "ok"), checks["findings"]
    assert checks["findings"] == []


@pytest.mark.parametrize("name", NAMES)
def test_an_example_gives_the_same_value_on_a_bar_whatever_comes_after(ex, name):
    """The design's no-lookahead test for the examples: computing on the bars up to k gives,
    at every bar up to k, what computing on all of them gives."""
    full = values(ex, name, limit=800).json()
    t = full["t"]
    for cut in (len(t) - 1, len(t) - 7, len(t) - 40, len(t) // 2):
        part = values(ex, name, **{"from": t[0], "to": t[cut] + 1, "limit": 800}).json()
        assert part["t"] == t[: cut + 1]
        for out, series in full["values"].items():
            for want, got in zip(series[: cut + 1], part["values"][out], strict=True):
                assert (want is None and got is None) or got == pytest.approx(want, rel=1e-9)
        for out, hits in full["hits"].items():
            assert part["hits"][out] == [i for i in hits if i <= cut], out


# Controls: the tests above can fail


def peeking(ex, name: str, source: str) -> str:
    (Path(ex.server.indicators_dir) / f"{name}.py").write_text(source, encoding="utf-8")
    return name


def test_control_a_peeking_variant_is_flagged_by_the_checks_and_the_prefix_test(ex):
    text = (EXAMPLES / "sma_cross.py").read_text(encoding="utf-8")
    peek = text.replace('"fast": fast,', '"fast": fast.shift(-3),')
    assert peek != text
    name = peeking(ex, "sma_cross_peek", peek)
    _, custom = catalog(ex)
    assert custom[name]["status"] == "ok"
    end = time.monotonic() + 30
    while (got := values(ex, name).json()["checks"])["lookahead"] == "pending":
        assert time.monotonic() < end
        time.sleep(0.05)
    assert got["lookahead"] == "warned" and got["findings"][0]["output"] == "fast"
    # and the values on a bar change when later bars are removed
    full = values(ex, name, limit=800).json()
    part = values(ex, name, **{"from": full["t"][0], "to": full["t"][-20] + 1, "limit": 800}).json()
    last = len(part["t"]) - 1
    assert part["values"]["fast"][last - 1] != full["values"]["fast"][last - 1]


def test_numpy_and_pandas_can_be_imported_by_a_file(ex):
    src = (
        "import numpy as np\nimport pandas as pd\n"
        "from openchart.indicators import Color, Line, Width\n"
        'NAME = "Np"\nTITLE = "Np"\nOVERLAY = True\n'
        'PARAMS = [Color("c", "#2962ff"), Width("w", 1)]\n'
        'OUTPUTS = [Line("x", "X", color="c", width="w")]\n'
        "def compute(bars, params):\n"
        '    return {"x": pd.Series(np.log(bars["close"].to_numpy()), index=bars.index)}\n'
    )
    name = peeking(ex, "np_ok", src)
    body = values(ex, name).json()
    assert all(v is not None for v in body["values"]["x"])


# The page


def example_blocks() -> dict[str, str]:
    """`<!-- example: name.py -->` followed by a python block, as the page writes them."""
    text = PAGE.read_text(encoding="utf-8")
    found = re.findall(r"<!-- example: (\w+\.py) -->\n```python\n(.*?)```", text, re.S)
    return dict(found)


def test_the_page_shows_every_example_verbatim():
    blocks = example_blocks()
    assert sorted(blocks) == [f"{n}.py" for n in NAMES]
    for name, shown in blocks.items():
        assert shown == (EXAMPLES / name).read_text(encoding="utf-8"), f"{name} on the page differs"


def test_the_pine_sheet_names_only_helpers_that_exist():
    text = PAGE.read_text(encoding="utf-8")
    named = set(re.findall(r"`ta\.([a-z_]+)\(", text))
    assert len(named) >= 20
    assert not [n for n in named if not callable(getattr(ta, n, None))], "unknown ta helper"


def test_the_page_lists_every_output_kind():
    text = PAGE.read_text(encoding="utf-8")
    for kind in OUTPUT_TYPES:
        assert f"`{kind.__name__}(" in text, kind.__name__


def test_the_page_documents_every_ta_helper_in_the_sheet_or_says_it_is_not_in_pine():
    """No helper is left out of the cheat sheet by accident."""
    text = PAGE.read_text(encoding="utf-8")
    public = {n for n in dir(ta) if not n.startswith("_") and callable(getattr(ta, n))}
    public -= {"Integral"}
    missing = sorted(n for n in public if f"ta.{n}" not in text)
    assert not missing, missing


def test_json_of_the_example_catalog_is_plain(ex):
    # The page quotes catalog fields; they must be JSON-serialisable as the API sends them.
    _, custom = catalog(ex)
    json.dumps(custom)
