"""Moving average convergence divergence: the MACD line, its signal and the histogram."""

from openchart.indicators import Choice, Color, Histogram, Int, Line, Source, Width, source, ta

NAME = "MACD"
TITLE = "Moving Average Convergence Divergence"
OVERLAY = False
PARAMS = [
    Int("fast", 12, min=1, max=900, label="Fast length"),
    Int("slow", 26, min=1, max=900, label="Slow length"),
    Int("signal", 9, min=1, max=900, label="Signal smoothing"),
    Source("source", "close"),
    Choice("osc_ma", "EMA", ["EMA", "SMA"], label="Oscillator MA type"),
    Choice("signal_ma", "EMA", ["EMA", "SMA"], label="Signal MA type"),
    Color("macd_color", "#2962ff", label="MACD colour"),
    Color("signal_color", "#ff6d00", label="Signal colour"),
    Color("hist_color", "#26a69a", label="Histogram colour"),
    Width("width", 1),
]
OUTPUTS = [
    Histogram("hist", "Histogram", color="hist_color"),
    Line("macd", "MACD", color="macd_color", width="width"),
    Line("signal", "Signal", color="signal_color", width="width"),
]

_MA = {"EMA": ta.ema, "SMA": ta.sma}


def warmup(params):
    # An EMA agrees with a longer history to 1e-8 after about 10 lengths (11 with the seed);
    # a simple average is exact from its first value. The two are chained: the signal averages
    # the line.
    slow = max(params["fast"], params["slow"])
    line = (11 if params["osc_ma"] == "EMA" else 1) * slow
    signal = (11 if params["signal_ma"] == "EMA" else 1) * params["signal"]
    return line + signal


def compute(bars, params):
    src = source(bars, params["source"])
    osc, smooth = _MA[params["osc_ma"]], _MA[params["signal_ma"]]
    line = osc(src, params["fast"]) - osc(src, params["slow"])
    signal = smooth(line, params["signal"])
    return {"macd": line, "signal": signal, "hist": line - signal}
