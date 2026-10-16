"""Bollinger-style bands: a moving average, a band of `mult` deviations around it, the band
shaded, and the bars that close outside it tinted."""

from openchart.indicators import (
    Background,
    Color,
    Fill,
    Float,
    Int,
    Line,
    Source,
    Width,
    source,
    ta,
)

NAME = "Bands"
TITLE = "Bollinger-style bands"
OVERLAY = True

PARAMS = [
    Int("length", 20, min=2, max=500),
    Float("mult", 2.0, min=0.1, max=10, label="Deviations"),
    Source("source", "close"),
    Color("mid_color", "#2962ff", label="Middle colour"),
    Color("band_color", "#ff6d00", label="Band colour"),
    Width("width", 1),
]
OUTPUTS = [
    Line("basis", "Middle", color="mid_color", width="width"),
    Line("upper", "Upper", color="band_color", width="width", style="dashed"),
    Line("lower", "Lower", color="band_color", width="width", style="dashed"),
    Fill(between=("upper", "lower"), color="band_color", opacity=0.1, label="Band"),
    Background("outside", "Close outside", color="band_color", opacity=0.1),
]


def warmup(params):
    return params["length"]


def compute(bars, params):
    src = source(bars, params["source"])
    basis, upper, lower = ta.bb(src, params["length"], params["mult"])
    return {
        "basis": basis,
        "upper": upper,
        "lower": lower,
        "outside": (src > upper) | (src < lower),
    }
