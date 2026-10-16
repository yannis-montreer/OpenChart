"""Demo (own pane): an RSI with its bands, a fill between them and a tint outside.

The overbought and oversold limits are inputs, so changing one in Settings moves its line.
The band's edges are two constant lines (a fill is drawn between two outputs that have
values); the middle is a level.
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

NAME = "Demo RSI"
TITLE = "Demo: RSI with bands"
OVERLAY = False
PARAMS = [
    Int("length", 14, min=2, max=200),
    Float("upper", 70, min=50, max=100, label="Overbought"),
    Float("lower", 30, min=0, max=50, label="Oversold"),
    Source(),
    Color("c_rsi", "#7e57c2", label="RSI"),
    Color("c_band", "#7e57c2", label="Band"),
    Color("c_high", "#f23645", label="Overbought"),
    Color("c_low", "#089981", label="Oversold"),
    Color("c_mid", "#787b86", label="Middle"),
    Width("w", 2, label="Width"),
    Width("w_edge", 1, label="Band width"),
]
OUTPUTS = [
    Line("rsi", "RSI", color="c_rsi", width="w"),
    Line("upper_edge", "Overbought", color="c_high", width="w_edge", style="dashed"),
    Line("lower_edge", "Oversold", color="c_low", width="w_edge", style="dashed"),
    Level(50, "Middle", color="c_mid", style="dotted"),
    Fill(between=("upper_edge", "lower_edge"), color="c_band", opacity=0.1, label="Band"),
    Background("hot", "Overbought", color="c_high", opacity=0.12),
    Background("cold", "Oversold", color="c_low", opacity=0.12),
    Marker("leave_high", "Back under", color="c_high", shape="arrow_down", position="above"),
    Marker("leave_low", "Back over", color="c_low", shape="arrow_up", position="below"),
]


def warmup(params):
    return 10 * params["length"] + 10


def compute(bars, params):
    rsi = ta.rsi(source(bars, params["source"]), params["length"])
    return {
        "rsi": rsi,
        "upper_edge": rsi * 0 + params["upper"],
        "lower_edge": rsi * 0 + params["lower"],
        "hot": rsi >= params["upper"],
        "cold": rsi <= params["lower"],
        "leave_high": ta.crossunder(rsi, params["upper"]),
        "leave_low": ta.crossover(rsi, params["lower"]),
    }
