"""Indicators (SPEC section 11): one file each, discovered at startup.

Built-in indicators are the modules of this package whose name does not start with `_`;
the owner's own are `.py` files in `<data dir>/indicators/`. What a file declares is
described in `_base.py` and docs/indicators.md. Indicator files import what they need
from here.
"""

from openchart.indicators import _ta as ta
from openchart.indicators._base import (
    SOURCES,
    Area,
    Background,
    Bool,
    Choice,
    Color,
    Fill,
    Float,
    Histogram,
    Indicator,
    Info,
    Int,
    Level,
    Line,
    Marker,
    Output,
    Param,
    Source,
    String,
    Width,
    source,
)
from openchart.indicators._ta import ema, sma

__all__ = [
    "SOURCES",
    "Area",
    "Background",
    "Bool",
    "Choice",
    "Color",
    "Fill",
    "Float",
    "Histogram",
    "Indicator",
    "Info",
    "Int",
    "Level",
    "Line",
    "Marker",
    "Output",
    "Param",
    "Source",
    "String",
    "Width",
    "ema",
    "sma",
    "source",
    "ta",
]
