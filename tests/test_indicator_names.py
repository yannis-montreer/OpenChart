"""A name that is not an identifier is refused with the rule and the character that breaks it
(owner, CI-2 b review)."""

import pytest

from openchart.indicators._base import SchemaError
from openchart.indicators.worker.child import _compile

TEMPLATE = """
from openchart.indicators import *
NAME = "N"
OVERLAY = False
PARAMS = [{params}Color("color", "#ff0000"), Width("width")]
OUTPUTS = [{outputs}]
def compute(bars, params):
    return {{}}
"""
LINE = 'Line("{name}", "L", color="color", width="width")'


def refused(**kw) -> str:
    source = TEMPLATE.format(**{"params": "", "outputs": LINE.format(name="v"), **kw})
    with pytest.raises(SchemaError) as caught:
        _compile("n.py", source)
    return str(caught.value)


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("my-input", "'-' at position 3 is not allowed"),
        ("my input", "' ' at position 3 is not allowed"),
        ("1st", "'1' at position 1 cannot start a name"),
        ("café", "'é' at position 4 is not allowed"),
        ("a" * 41, "it has 41"),
        ("", "it is empty"),
        ("x.y", "'.' at position 2 is not allowed"),
    ],
)
def test_an_input_name_that_is_not_an_identifier_says_which_character(name, detail):
    message = refused(params=f"Int({name!r}, 1), ")
    assert message.startswith("n.py: input ")
    assert "must be an identifier (letters, digits and _, starting with a letter or _" in message
    assert "at most 40 characters" in message
    assert detail in message, message


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("up-arrow", "'-' at position 3 is not allowed"),
        ("9lives", "'9' at position 1"),
        ("b" * 41, "it has 41"),
    ],
)
def test_an_output_name_that_is_not_an_identifier_says_which_character(name, detail):
    message = refused(outputs=LINE.format(name=name))
    assert message.startswith("n.py: output ")
    assert detail in message, message


def test_a_trailing_newline_is_not_part_of_an_identifier():
    assert "'\\n' at position 2" in refused(params='Int("a\\n", 1), ')


def test_the_limits_themselves_are_fine():
    for name in ("_", "a" * 40, "A1_b2"):
        source = TEMPLATE.format(params=f'Int("{name}", 1), ', outputs=LINE.format(name="v"))
        _compile("n.py", source)
