"""Blank: one line of the closes, the smallest file that loads. Start from here."""

from openchart.indicators import Color, Line, Width

NAME = "Blank"
TITLE = "Blank"
OVERLAY = True

PARAMS = [Color("color", "#2962ff"), Width("width", 1)]
OUTPUTS = [Line("value", "Value", color="color", width="width")]


def compute(bars, params):
    return {"value": bars["close"]}
