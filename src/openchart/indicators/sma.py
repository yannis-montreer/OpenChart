"""Simple moving average, as TradingView's `ta.sma`."""

from openchart.indicators import Color, Int, Output, Source, Width, sma, source

NAME = "SMA"
TITLE = "Moving Average Simple"
OVERLAY = True
PARAMS = [
    Int("length", 9, min=1, max=5000),
    Source("source", "close"),
    Color("color", "#2962ff", label="Colour"),
    Width("width", 1),
]
OUTPUTS = [Output("sma", "SMA", color="color", width="width")]


def warmup(params):
    # Exact from the first value: the average of the last `length` bars.
    return params["length"]


def compute(bars, params):
    return {"sma": sma(source(bars, params["source"]), params["length"])}
