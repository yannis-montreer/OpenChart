"""Indicators (SPEC section 11): the EMA against a reference, the file interface and
discovery, and values from the API aligned to the candles of the chart's price type."""

import json
import math
import random

import numpy as np
import pandas as pd
import pytest

from openchart.indicators import ema, sma, source
from openchart.indicators._registry import load_indicators

# EMA


def reference_ema(values: list[float], length: int) -> list[float | None]:
    """The textbook recurrence, written out: seed with the simple average of the first
    `length` values (as TradingView's ta.ema does), then alpha = 2 / (length + 1)."""
    alpha = 2 / (length + 1)
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def same(got: pd.Series, want: list[float | None]) -> None:
    assert len(got) == len(want)
    for g, w in zip(got.tolist(), want, strict=True):
        if w is None:
            assert math.isnan(g)
        else:
            assert g == pytest.approx(w, rel=1e-12, abs=1e-12)


@pytest.mark.parametrize("length", [1, 2, 3, 9, 21, 200])
def test_ema_matches_the_reference(length):
    rng = random.Random(length)
    values = [2000 + rng.gauss(0, 5) for _ in range(600)]
    same(ema(pd.Series(values), length), reference_ema(values, length))


def test_ema_by_hand():
    # length 3: alpha 0.5, seed (1 + 2 + 3) / 3 = 2, then 0.5 * 4 + 0.5 * 2 = 3, then 4.
    same(ema(pd.Series([1.0, 2, 3, 4, 5]), 3), [None, None, 2.0, 3.0, 4.0])
    same(ema(pd.Series([1.0, 2]), 3), [None, None])  # not enough bars yet


def test_ema_composes_over_another_indicators_warm_up():
    values = pd.Series([float(x) for x in range(1, 21)])
    smoothed = ema(sma(values, 4), 3)  # sma is NaN for 3 bars, so the EMA seeds 3 later
    assert smoothed.iloc[:5].isna().all()
    assert smoothed.iloc[5] == pytest.approx(sma(values, 4).iloc[3:6].mean())


def test_sources():
    bars = pd.DataFrame({"o": [1.0], "h": [4.0], "l": [0.0], "c": [2.0]})
    got = {name: source(bars, name).iloc[0] for name in ("open", "hl2", "hlc3", "ohlc4", "hlcc4")}
    assert got == {"open": 1.0, "hl2": 2.0, "hlc3": 2.0, "ohlc4": 1.75, "hlcc4": 2.0}


# The built-in files (yours: tests/test_custom_indicators.py)


def test_the_builtin_ema_and_its_schema():
    registry = load_indicators()
    assert registry.errors == []
    # built-in only: your files go to the worker
    assert sorted(registry.indicators) == sorted(["ema", *BUILTIN_REST])
    described = registry.get("ema").to_json()
    assert described["overlay"] is True
    assert [(p["name"], p["type"], p["default"], p["group"]) for p in described["params"]] == [
        ("length", "int", 9, "inputs"),
        ("source", "source", "close", "inputs"),
        ("color", "color", "#2962ff", "style"),
        ("width", "width", 1, "style"),
    ]
    # The M7 fields are unchanged; CI-2 (b) adds the output's kind and line style.
    assert described["outputs"] == [
        {
            "kind": "line",
            "name": "ema",
            "label": "EMA",
            "color": "color",
            "width": "width",
            "style": "solid",
        }
    ]


def test_parameters_are_validated_and_completed():
    ema_ind = load_indicators().get("ema")
    assert ema_ind.normalize({"length": 20.0}) == {
        "length": 20,
        "source": "close",
        "color": "#2962ff",
        "width": 1,
    }
    for bad, why in [
        ({"length": 0}, "at least 1"),
        ({"length": 2.5}, "whole number"),
        ({"source": "volume"}, "must be one of"),
        ({"color": "blue"}, "colour"),
        ({"width": 9}, "at most 4"),
        ({"offset": 1}, "no parameter 'offset'"),
    ]:
        with pytest.raises(ValueError, match=why):
            ema_ind.normalize(bad)


# Through the API


def candles(client, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", **query}
    return client.get("/api/candles", params=q).json()["candles"]


BUILTIN_REST = ["sma", "rsi", "macd", "bollinger", "atr", "stochastic", "volume"]
# Average True Range, Bollinger Bands, Moving Average Convergence Divergence, ... Exponential,
# ... Simple, Relative Strength Index, Stochastic, Volume (tick volume)
IN_MENU_ORDER = ["atr", "bollinger", "macd", "ema", "sma", "rsi", "stochastic", "volume"]


def values(client, indicator="ema", params=None, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": json.dumps(params or {}), **query}
    return client.get(f"/api/indicators/{indicator}", params=q)


def test_the_catalog(client):
    body = client.get("/api/indicators").json()
    assert [i["id"] for i in body["indicators"]] == IN_MENU_ORDER  # by title
    assert body["errors"] == [] and body["folder"].endswith("indicators")
    assert [i["id"] for i in body["builtin"]] == IN_MENU_ORDER and body["custom"] == []
    assert body["isolation"]["level"] == "unavailable"  # the test server runs no worker
    assert body["editing"]["allowed"] is False


def test_values_are_aligned_to_the_candles_and_settle_with_warm_up(client):
    bars = candles(client, limit=300)
    body = values(client, params={"length": 9}, limit=300).json()
    assert body["t"] == [b["t"] for b in bars]
    assert body["params"] == {"length": 9, "source": "close", "color": "#2962ff", "width": 1}

    # The same bars computed over the whole stored history: warm-up makes them agree.
    everything = candles(client, **{"from": 0, "limit": 5000})
    full = reference_ema([b["c"] for b in everything], 9)
    by_time = dict(zip([b["t"] for b in everything], full, strict=True))
    for t, v in zip(body["t"], body["values"]["ema"], strict=True):
        assert v == pytest.approx(by_time[t], rel=1e-9)


def test_the_first_bars_of_history_are_seeded_with_the_simple_average(client):
    first = candles(client, **{"from": 0, "limit": 30})
    body = values(client, params={"length": 9, "source": "hl2"}, **{"from": 0, "limit": 30}).json()
    hl2 = [(b["h"] + b["l"]) / 2 for b in first]
    assert body["values"]["ema"] == pytest.approx(reference_ema(hl2, 9))
    assert body["values"]["ema"][:8] == [None] * 8
    assert body["values"]["ema"][8] == pytest.approx(np.mean(hl2[:9]))


@pytest.mark.parametrize("price", ["bid", "ask", "mid"])
def test_values_follow_the_price_type(client, price):
    q = {"from": 0, "limit": 50, "price": price}
    closes = [b["c"] for b in candles(client, **q)]
    body = values(client, params={"length": 5}, **q).json()
    assert body["price"] == price
    assert body["values"]["ema"] == pytest.approx(reference_ema(closes, 5))


def test_bid_and_ask_values_differ(client):
    q = {"from": 0, "limit": 20}
    bid = values(client, price="bid", **q).json()["values"]["ema"][-1]
    ask = values(client, price="ask", **q).json()["values"]["ema"][-1]
    assert ask > bid


@pytest.mark.parametrize(
    ("indicator", "query", "status"),
    [
        ("nope", {}, 404),
        ("ema", {"kind": "custom"}, 404),
        ("ema", {"kind": "neither"}, 400),
        ("ema", {"params": json.dumps({"length": 0})}, 400),
        ("ema", {"params": "[1]"}, 400),
        ("ema", {"params": "{"}, 400),
        ("ema", {"timeframe": "7m"}, 400),
        ("ema", {"price": "last"}, 400),
        ("ema", {"instrument": "NOPE"}, 404),
    ],
)
def test_errors(client, indicator, query, status):
    q = {"instrument": "XAU_USD", "timeframe": "5m", **query}
    assert client.get(f"/api/indicators/{indicator}", params=q).status_code == status
