"""Demo (own pane): every output kind in one file, for looking at the chart.

An EMA difference with a signal line: three line styles, a histogram, an area, a fill
between two lines, a fixed level and one that follows an input, markers of all four
shapes and all three positions with text, and a tinted background.
"""

from openchart.indicators import (
    Area,
    Background,
    Color,
    Fill,
    Float,
    Histogram,
    Int,
    Level,
    Line,
    Marker,
    Source,
    Width,
    source,
    ta,
)

NAME = "Demo kinds"
TITLE = "Demo: every output kind"
OVERLAY = False
PARAMS = [
    Int("fast", 12, min=2, max=200),
    Int("slow", 26, min=3, max=400),
    Int("signal", 9, min=2, max=100),
    Float("limit", 2.0, min=0, label="Limit"),
    Source(),
    Color("c_main", "#2962ff", label="Main"),
    Color("c_signal", "#ff6d00", label="Signal"),
    Color("c_slow", "#9c27b0", label="Slow average"),
    Color("c_hist", "#787b86", label="Histogram"),
    Color("c_up", "#089981", label="Up"),
    Color("c_down", "#f23645", label="Down"),
    Color("c_level", "#b2b5be", label="Levels"),
    Width("w", 2, label="Width"),
]
OUTPUTS = [
    Line("main", "Main (solid)", color="c_main", width="w"),
    Line("signal", "Signal (dashed)", color="c_signal", width="w", style="dashed"),
    Line("slow", "Slow (dotted)", color="c_slow", width="w", style="dotted"),
    Histogram("hist", "Histogram", color="c_hist", base=0),
    Area("area", "Area", color="c_slow"),
    Fill(between=("main", "signal"), color="c_main", opacity=0.18, label="Band"),
    Level(0, "Zero", color="c_level", style="dotted"),
    Level("limit", "Limit", color="c_down", width="w"),
    Marker("up", "Cross up", color="c_up", shape="arrow_up", position="below", text="up"),
    Marker("down", "Cross down", color="c_down", shape="arrow_down", position="above", text="down"),
    Marker("flip", "Zero cross", color="c_signal", shape="circle", position="on"),
    Marker("big", "Passes limit", color="c_main", shape="square", position="above", text="!"),
    Background("bull", "Above zero", color="c_up", opacity=0.08),
]


def warmup(params):
    return 10 * params["slow"] + 10


def compute(bars, params):
    src = source(bars, params["source"])
    main = ta.ema(src, params["fast"]) - ta.ema(src, params["slow"])
    signal = ta.ema(main, params["signal"])
    above = main > 0
    return {
        "main": main,
        "signal": signal,
        "slow": ta.sma(main, 4 * params["signal"]),
        "hist": main - signal,
        "area": ta.sma(main, 4 * params["signal"]),
        "up": ta.crossover(main, signal),
        "down": ta.crossunder(main, signal),
        "flip": ta.cross(main, 0),
        "big": ta.crossover(main.abs(), params["limit"]),
        "bull": above,
    }
