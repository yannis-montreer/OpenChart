"""Exponential moving average, seeded with a simple average like TradingView's `ta.ema`."""

from openchart.indicators import Color, Int, Output, Source, Width, ema, source

NAME = "EMA"
TITLE = "Moving Average Exponential"
OVERLAY = True
PARAMS = [
    Int("length", 9, min=1, max=5000),
    Source("source", "close"),
    Color("color", "#2962ff", label="Colour"),
    Width("width", 1),
]
OUTPUTS = [Output("ema", "EMA", color="color", width="width")]


def warmup(params):
    # The seed's weight after n bars is (1 - alpha) ** n: below 1e-8 after 10 lengths.
    return 10 * params["length"] + 10


def compute(bars, params):
    return {"ema": ema(source(bars, params["source"]), params["length"])}
