"""RSI with bands: the line, overbought and oversold levels you can change, a tint while
it is beyond them, and an arrow when it comes back inside."""

from openchart.indicators import (
    Background,
    Color,
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

NAME = "RSI bands"
TITLE = "RSI with overbought and oversold bands"
OVERLAY = False

PARAMS = [
    Int("length", 14, min=2, max=200),
    Float("upper", 70, min=50, max=100, label="Overbought"),
    Float("lower", 30, min=0, max=50, label="Oversold"),
    Source("source", "close"),
    Color("rsi_color", "#7e57c2", label="RSI colour"),
    Width("width", 2),
    Color("high_color", "#ef5350", label="Overbought colour"),
    Color("low_color", "#26a69a", label="Oversold colour"),
    Color("mid_color", "#787b86", label="Middle colour"),
]
OUTPUTS = [
    Line("rsi", "RSI", color="rsi_color", width="width"),
    Level("upper", "Overbought", color="high_color"),
    Level(50, "Middle", color="mid_color", style="dotted"),
    Level("lower", "Oversold", color="low_color"),
    Background("hot", "Overbought", color="high_color", opacity=0.12),
    Background("cold", "Oversold", color="low_color", opacity=0.12),
    Marker("back_down", "Back inside", color="high_color", shape="arrow_down", position="above"),
    Marker("back_up", "Back inside", color="low_color", shape="arrow_up", position="below"),
]


def warmup(params):
    # RSI is seeded with an average and then smoothed: about 20 lengths agree to 1e-8.
    return 20 * params["length"]


def compute(bars, params):
    rsi = ta.rsi(source(bars, params["source"]), params["length"])
    return {
        "rsi": rsi,
        "hot": rsi >= params["upper"],
        "cold": rsi <= params["lower"],
        "back_down": ta.crossunder(rsi, params["upper"]),
        "back_up": ta.crossover(rsi, params["lower"]),
    }
