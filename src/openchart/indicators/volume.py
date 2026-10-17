"""Volume as columns. OANDA gives no real volume: this is the TICK volume, the number of
price updates in each bar."""

from openchart.indicators import Color, Histogram

NAME = "Volume"
TITLE = "Volume (tick volume)"
OVERLAY = False
PARAMS = [Color("color", "#26a69a", label="Colour")]
OUTPUTS = [Histogram("volume", "Tick volume", color="color")]


def compute(bars, params):
    return {"volume": bars.v}
