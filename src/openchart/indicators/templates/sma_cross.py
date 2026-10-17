"""SMA cross: two moving averages and a marker where they cross."""

from openchart.indicators import Color, Int, Line, Marker, Source, Width, source, ta

NAME = "SMA cross"
TITLE = "SMA cross"
OVERLAY = True

PARAMS = [
    Int("fast", 10, min=1, max=500),
    Int("slow", 30, min=2, max=2000),
    Source("source", "close"),
    Color("fast_color", "#2962ff", label="Fast colour"),
    Color("slow_color", "#ff6d00", label="Slow colour"),
    Width("width", 1),
    Color("up_color", "#26a69a", label="Up colour"),
    Color("down_color", "#ef5350", label="Down colour"),
]
OUTPUTS = [
    Line("fast", "Fast", color="fast_color", width="width"),
    Line("slow", "Slow", color="slow_color", width="width"),
    Marker("up", "Cross up", color="up_color", shape="arrow_up", position="below"),
    Marker("down", "Cross down", color="down_color", shape="arrow_down", position="above"),
]


def warmup(params):
    # A cross needs both averages on the bar before it, so one bar more than the slow one.
    return params["slow"] + 1


def compute(bars, params):
    src = source(bars, params["source"])
    fast, slow = ta.sma(src, params["fast"]), ta.sma(src, params["slow"])
    return {
        "fast": fast,
        "slow": slow,
        "up": ta.crossover(fast, slow),
        "down": ta.crossunder(fast, slow),
    }
