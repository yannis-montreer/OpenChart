"""Bollinger Bands: a simple average and bands of population deviations either side."""

from openchart.indicators import Color, Fill, Float, Int, Line, Source, Width, source, ta

NAME = "BB"
TITLE = "Bollinger Bands"
OVERLAY = True
PARAMS = [
    Int("length", 20, min=1, max=5000),
    Source("source", "close"),
    Float("mult", 2.0, min=0.001, max=50, step=0.1, label="StdDev"),
    Color("basis_color", "#ff6d00", label="Basis colour"),
    Color("band_color", "#2962ff", label="Bands colour"),
    Width("width", 1),
]
OUTPUTS = [
    Line("basis", "Basis", color="basis_color", width="width"),
    Line("upper", "Upper", color="band_color", width="width"),
    Line("lower", "Lower", color="band_color", width="width"),
    Fill(between=("upper", "lower"), color="band_color", opacity=0.1, label="Background"),
]


def warmup(params):
    # Exact from the first value: a window of `length` bars.
    return params["length"]


def compute(bars, params):
    basis, upper, lower = ta.bb(source(bars, params["source"]), params["length"], params["mult"])
    return {"basis": basis, "upper": upper, "lower": lower}
