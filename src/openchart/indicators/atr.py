"""Average true range, smoothed with RMA (TradingView's default), SMA, EMA or WMA."""

from openchart.indicators import Choice, Color, Int, Line, Width, ta

NAME = "ATR"
TITLE = "Average True Range"
OVERLAY = False
PARAMS = [
    Int("length", 14, min=1, max=900),
    Choice("smoothing", "RMA", ["RMA", "SMA", "EMA", "WMA"]),
    Color("color", "#b71c1c", label="Colour"),
    Width("width", 1),
]
OUTPUTS = [Line("atr", "ATR", color="color", width="width")]

_SMOOTHERS = {"RMA": ta.rma, "SMA": ta.sma, "EMA": ta.ema, "WMA": ta.wma}


def warmup(params):
    n = params["length"]
    # The windowed averages are exact from their first value; the recursive ones fade.
    if params["smoothing"] == "RMA":
        return 20 * n + n + 10
    if params["smoothing"] == "EMA":
        return 10 * n + n + 10
    return n


def compute(bars, params):
    true_range = ta.tr(bars.h, bars.l, bars.c)
    return {"atr": _SMOOTHERS[params["smoothing"]](true_range, params["length"])}
