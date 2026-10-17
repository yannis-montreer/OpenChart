"""A marker on a pattern: an engulfing candle.

A bullish engulfing bar closes up, after a down bar, with a body that covers the whole body
of that bar. A bearish one is the mirror image. Only the bar and the one before it are read,
so there is nothing to look ahead to and no warmup beyond that one bar.
"""

from openchart.indicators import Color, Marker, ta

NAME = "Engulfing"
TITLE = "Engulfing candles"
OVERLAY = True

PARAMS = [
    Color("bull_color", "#26a69a", label="Bullish colour"),
    Color("bear_color", "#ef5350", label="Bearish colour"),
]
OUTPUTS = [
    Marker("bull", "Bullish", color="bull_color", shape="arrow_up", position="below", text="E"),
    Marker("bear", "Bearish", color="bear_color", shape="arrow_down", position="above", text="E"),
]


def warmup(params):
    return 1


def compute(bars, params):
    o, c = bars["open"], bars["close"]
    prev_o, prev_c = ta.shift(o, 1), ta.shift(c, 1)
    bull = (c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)
    bear = (c < o) & (prev_c > prev_o) & (c <= prev_o) & (o >= prev_c)
    # Gaps (the first bar) compare False, which is what a marker needs: never NaN.
    return {"bull": bull.fillna(False).astype(bool), "bear": bear.fillna(False).astype(bool)}
