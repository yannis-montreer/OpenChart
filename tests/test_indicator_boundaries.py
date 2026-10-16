"""Values exactly on a limit load (a mutation check found the upper end of an opacity
unpinned)."""

import pytest

from openchart.indicators.worker.child import _compile

TEMPLATE = """
from openchart.indicators import *
NAME = "B"
OVERLAY = False
PARAMS = [Color("color", "#ff0000"), Width("width")]
OUTPUTS = [
    Line("a", "A", color="color", width="width"),
    Line("b", "B", color="color", width="width"),
    {output}
]
def compute(bars, params):
    return {{}}
"""


@pytest.mark.parametrize(
    "output",
    [
        'Fill(between=("a", "b"), color="color", opacity=1)',
        'Fill(between=("a", "b"), color="color", opacity=0.0001)',
        'Background("bg", "Bg", color="color", opacity=1)',
        'Background("bg", "Bg", color="color", opacity=0.0001)',
    ],
)
def test_an_opacity_of_exactly_one_or_just_above_zero_loads(output):
    _compile("b.py", TEMPLATE.format(output=output))
