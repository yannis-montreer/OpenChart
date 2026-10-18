"""The warmup notice and the two caches (#47 after #46 and #49): a cached answer never
hides a notice, never repeats one that does not apply, and never carries values computed
on another warmup.

The values cache key must hold everything the warmup cut depends on (the indicator, its
inputs, the bars asked for, the price type), and a write to the store (the bar cache's
version counter) must still lead to new values. Each answer from a warm cache is compared
with the answer from an empty one. tools/mutate_values_key.py breaks the key and the notice
one way at a time; these tests must fail on each.
"""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.api.routes_indicators import MAX_WARMUP
from openchart.indicators._cache import ValueCache
from tests.conftest import CANDLES, NOW, Harness, run_setup
from tests.test_custom_indicators import wait_for_worker, write
from tests.worker_helpers import FakeLauncher

TO = NOW - 3600  # a fixed range: no forming bar, the same bars every time
CUT = '{"length": 2000}'  # EMA warmup 20,010: cut to 20,000
FITS = '{"length": 1999}'  # EMA warmup 20,000: fits
EARLY = CANDLES[0].ts + 1500 * 60  # 1,200 bars before the 300 shown: any warmup takes them all


@pytest.fixture
def nc(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder = harness.server, folder
        assert run_setup(c).status_code == 200
        yield c


def ask(c, indicator, params, **query):
    q = {"instrument": "XAU_USD", "timeframe": "1m", "params": params, "limit": 300, "to": TO}
    r = c.get(f"/api/indicators/{indicator}", params={**q, **query})
    assert r.status_code == 200, r.text
    return r.json()


def fresh(c, indicator, params, **query):
    """The same request answered from an empty values cache (the warm one is kept)."""
    kept = c.server.values_cache
    c.server.values_cache = ValueCache()
    try:
        return ask(c, indicator, params, **query)
    finally:
        c.server.values_cache = kept


# (first request, second request): they differ in exactly one thing the cut depends on.
PAIRS = {
    "indicator": (("ema", CUT, {}), ("sma", CUT, {})),
    "indicator, the same bars": (("ema", CUT, {"to": EARLY}), ("sma", CUT, {"to": EARLY})),
    "inputs, cut then fits": (("ema", CUT, {}), ("ema", FITS, {})),
    "inputs, fits then cut": (("ema", FITS, {}), ("ema", CUT, {})),
    "bar range": (("ema", CUT, {}), ("ema", CUT, {"to": TO - 3600})),
    "price type": (("ema", CUT, {}), ("ema", CUT, {"price": "bid"})),
}


@pytest.mark.parametrize("name", list(PAIRS))
def test_a_cached_answer_equals_a_fresh_one_notice_included(nc, name):
    (a, pa, qa), (b, pb, qb) = PAIRS[name]
    first = ask(nc, a, pa, **qa)
    second = ask(nc, b, pb, **qb)
    assert second == fresh(nc, b, pb, **qb)
    assert second["values"] != first["values"]  # the pair really differs
    for body, ind, params in ((first, a, pa), (second, b, pb)):
        if ind == "ema" and params == CUT:
            assert body["notice"]["needed"] == 20_010
            assert body["notice"]["limit"] == MAX_WARMUP
        else:
            assert "notice" not in body
    again = ask(nc, a, pa, **qa)  # the first, from the cache now
    assert again == first


def test_a_range_without_bars_is_an_empty_answer_with_its_notice(nc):
    """Before the key moved into `values_key`, the forming window read the first bar of an
    empty frame: a 500."""
    body = ask(nc, "ema", CUT, to=CANDLES[0].ts - 86400)
    assert body["t"] == [] and body["values"] == {"ema": []}
    assert body["notice"]["needed"] == 20_010


def test_a_store_write_still_gives_new_values_and_keeps_the_notice(nc):
    """A repair or refill rewrites bars: the store's version counter cuts the bar cache,
    the values cache sees other bars (another key), and the notice stays."""
    first = ask(nc, "ema", CUT)
    assert ask(nc, "ema", CUT) == first  # both caches warm
    t = first["t"][-10]
    changed = [
        dataclasses.replace(c, mid=tuple(x + 1 for x in c.mid), bid=tuple(x + 1 for x in c.bid))
        for c in CANDLES
        if t <= c.ts < t + 600
    ]
    store = nc.server.store
    before = store.epoch(nc.server.source, "XAU_USD")
    nc.portal.call(store.upsert_m1, nc.server.source, "XAU_USD", changed)
    assert store.epoch(nc.server.source, "XAU_USD") != before
    second = ask(nc, "ema", CUT)
    assert second["values"]["ema"][-10] != first["values"]["ema"][-10]
    assert second == fresh(nc, "ema", CUT)
    assert second["notice"] == first["notice"]


def test_the_price_type_alone_is_another_key(nc):
    """Bid equal to mid: the same bars for both, so only the key's price type (in `info`)
    keeps a file that reads it from getting the other price's values."""
    wait_for_worker(nc)
    write(nc.folder, "priced.py", "PRICE WARMUP 30000\n")
    same = [dataclasses.replace(c, bid=c.mid, ask=c.mid) for c in CANDLES]
    nc.portal.call(nc.server.store.upsert_m1, nc.server.source, "XAU_USD", same)
    mid = ask(nc, "priced", "{}", kind="custom")
    bid = ask(nc, "priced", "{}", kind="custom", price="bid")
    assert bid == fresh(nc, "priced", "{}", kind="custom", price="bid")
    assert bid["values"]["out"][0] == mid["values"]["out"][0] + 1000
    for body in (mid, bid):
        assert body["notice"]["needed"] == 30_000
        assert body["checks"]["warmup_cut"]["needed"] == 30_000
