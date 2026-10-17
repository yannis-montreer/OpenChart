"""Stochastic oscillator: %K (smoothed) and %D, with overbought and oversold levels."""

from openchart.indicators import Color, Float, Int, Level, Line, Width, ta

NAME = "Stoch"
TITLE = "Stochastic"
OVERLAY = False
PARAMS = [
    Int("k_length", 14, min=1, max=5000, label="%K length"),
    Int("k_smooth", 1, min=1, max=5000, label="%K smoothing"),
    Int("d_smooth", 3, min=1, max=5000, label="%D smoothing"),
    Float("overbought", 80, min=0, max=100, label="Overbought level"),
    Float("oversold", 20, min=0, max=100, label="Oversold level"),
    Color("k_color", "#2962ff", label="%K colour"),
    Color("d_color", "#ff6d00", label="%D colour"),
    Width("width", 1),
    Color("level_color", "#787b86", label="Level colour"),
]
OUTPUTS = [
    Line("k", "%K", color="k_color", width="width"),
    Line("d", "%D", color="d_color", width="width"),
    Level("overbought", "Overbought", color="level_color"),
    Level("oversold", "Oversold", color="level_color"),
]


def warmup(params):
    # Windows only, exact from the first value: the three lengths in a row.
    return params["k_length"] + params["k_smooth"] + params["d_smooth"]


def compute(bars, params):
    raw = ta.stoch(bars.c, bars.h, bars.l, params["k_length"])
    k = ta.sma(raw, params["k_smooth"])
    return {"k": k, "d": ta.sma(k, params["d_smooth"])}
