"""Relative strength index with overbought and oversold levels, as TradingView's `ta.rsi`."""

from openchart.indicators import Color, Float, Int, Level, Line, Source, Width, source, ta

NAME = "RSI"
TITLE = "Relative Strength Index"
OVERLAY = False
PARAMS = [
    Int("length", 14, min=1, max=900),
    Source("source", "close"),
    Float("overbought", 70, min=0, max=100, label="Overbought level"),
    Float("oversold", 30, min=0, max=100, label="Oversold level"),
    Float("middle", 50, min=0, max=100, label="Middle level"),
    Color("color", "#7e57c2", label="Colour"),
    Width("width", 1),
    Color("level_color", "#787b86", label="Level colour"),
]
OUTPUTS = [
    Line("rsi", "RSI", color="color", width="width"),
    Level("overbought", "Overbought", color="level_color"),
    Level("oversold", "Oversold", color="level_color"),
    # Last, so the ids of the other levels stay where they were.
    Level("middle", "Middle", color="level_color"),
]


def warmup(params):
    # The Wilder average inside fades as (1 - 1/length) ** bars: 20 lengths agree to 1e-8.
    return 20 * params["length"] + params["length"] + 10


def compute(bars, params):
    return {"rsi": ta.rsi(source(bars, params["source"]), params["length"])}
