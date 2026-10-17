"""Moving average convergence divergence: the MACD line, its signal and the histogram."""

from openchart.indicators import Color, Histogram, Int, Line, Source, Width, source, ta

NAME = "MACD"
TITLE = "Moving Average Convergence Divergence"
OVERLAY = False
PARAMS = [
    Int("fast", 12, min=1, max=5000, label="Fast length"),
    Int("slow", 26, min=1, max=5000, label="Slow length"),
    Int("signal", 9, min=1, max=5000, label="Signal smoothing"),
    Source("source", "close"),
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


def warmup(params):
    # Two chained exponential averages: 10 lengths of the slower agree to 1e-8.
    slow = max(params["fast"], params["slow"])
    return 10 * (slow + params["signal"]) + slow + params["signal"]


def compute(bars, params):
    line, signal, hist = ta.macd(
        source(bars, params["source"]), params["fast"], params["slow"], params["signal"]
    )
    return {"macd": line, "signal": signal, "hist": hist}
