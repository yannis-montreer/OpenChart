"""Bollinger Bands: a moving average and bands of population deviations either side."""

from openchart.indicators import Choice, Color, Fill, Float, Int, Line, Source, Width, source, ta

NAME = "BB"
TITLE = "Bollinger Bands"
OVERLAY = True
PARAMS = [
    Int("length", 20, min=1, max=900),
    Source("source", "close"),
    Float("mult", 2.0, min=0.001, max=50, step=0.1, label="StdDev"),
    Choice("ma_type", "SMA", ["SMA", "EMA", "RMA", "WMA", "VWMA"], label="Basis MA type"),
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
    # The deviation is a window of `length` bars whatever the basis; the basis is exact from
    # its first value for SMA, WMA and VWMA and fades for EMA (about 10 lengths) and RMA (20).
    n = params["length"]
    return {"EMA": 11 * n + 10, "RMA": 21 * n + 10}.get(params["ma_type"], n)


def compute(bars, params):
    n, kind = params["length"], params["ma_type"]
    src = source(bars, params["source"])
    if kind == "VWMA":
        basis = ta.vwma(src, bars.v, n)
    else:
        basis = {"SMA": ta.sma, "EMA": ta.ema, "RMA": ta.rma, "WMA": ta.wma}[kind](src, n)
    spread = params["mult"] * ta.stdev(src, n)
    return {"basis": basis, "upper": basis + spread, "lower": basis - spread}
