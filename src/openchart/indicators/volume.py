"""Volume as columns, with an optional moving average. OANDA gives no real volume: this is
the TICK volume, the number of price updates in each bar."""

from openchart.indicators import Bool, Color, Histogram, Int, Line, Width, ta

NAME = "Volume"
TITLE = "Volume (tick volume)"
OVERLAY = False
PARAMS = [
    Bool("show_ma", False, label="Show moving average"),
    Int("ma_length", 20, min=1, max=5000, label="Moving average length"),
    Color("color", "#26a69a", label="Colour"),
    Color("ma_color", "#ff6d00", label="Moving average colour"),
    Width("ma_width", 1, label="Moving average width"),
]
OUTPUTS = [
    Histogram("volume", "Tick volume", color="color"),
    # Always declared (the outputs are fixed); all gaps while show_ma is off.
    Line("ma", "Tick volume MA", color="ma_color", width="ma_width"),
]


def warmup(params):
    return params["ma_length"] if params["show_ma"] else 0


def compute(bars, params):
    average = ta.sma(bars.v, params["ma_length"])
    if not params["show_ma"]:
        average = average * float("nan")
    return {"volume": bars.v, "ma": average}
