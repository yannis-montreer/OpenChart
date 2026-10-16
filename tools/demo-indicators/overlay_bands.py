"""Demo (price pane): bands around the price, markers on the candles, a tint and a level.

A moving average with a band of two deviations (a fill), dashed edges, a level at a price
you type in, markers above, below and on the bars, and a tinted background while the close
is outside the band.
"""

from openchart.indicators import (
    Background,
    Color,
    Fill,
    Float,
    Int,
    Level,
    Line,
    Marker,
    Source,
    Width,
    source,
    ta,
)

NAME = "Demo bands"
TITLE = "Demo: bands on the price"
OVERLAY = True
PARAMS = [
    Int("length", 20, min=2, max=500),
    Float("mult", 2.0, min=0.1, max=10, label="Deviations"),
    Float("price", 2000.0, min=0, label="Level price"),
    Source(),
    Color("c_mid", "#2962ff", label="Middle"),
    Color("c_edge", "#ff6d00", label="Edges"),
    Color("c_up", "#089981", label="Up"),
    Color("c_down", "#f23645", label="Down"),
    Color("c_level", "#b2b5be", label="Level"),
    Width("w", 1, label="Width"),
]
OUTPUTS = [
    Line("mid", "Middle", color="c_mid", width="w"),
    Line("upper", "Upper", color="c_edge", width="w", style="dashed"),
    Line("lower", "Lower", color="c_edge", width="w", style="dotted"),
    Fill(between=("upper", "lower"), color="c_edge", opacity=0.1, label="Band"),
    Level("price", "Price", color="c_level", style="dashed"),
    Marker("above", "Closed above", color="c_up", shape="arrow_up", position="below", text="out"),
    Marker(
        "below", "Closed below", color="c_down", shape="arrow_down", position="above", text="out"
    ),
    Marker("touch", "On the middle", color="c_mid", shape="circle", position="on"),
    Marker("sq", "Both edges", color="c_edge", shape="square", position="above"),
    Background("outside", "Outside", color="c_edge", opacity=0.1),
]


def warmup(params):
    return params["length"] + 2


def compute(bars, params):
    src = source(bars, params["source"])
    mid = ta.sma(src, params["length"])
    dev = ta.stdev(src, params["length"]) * params["mult"]
    upper, lower = mid + dev, mid - dev
    close = bars["close"]
    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "above": ta.crossover(close, upper),
        "below": ta.crossunder(close, lower),
        "touch": ta.cross(close, mid),
        "sq": (bars["high"] > upper) & (bars["low"] < lower),
        "outside": (close > upper) | (close < lower),
    }
