"""The indicator schema is checked when a file loads (CI-2 step b, design 2.1 and 2.3).

Each rule has a file that breaks it and the message it must give: the file, the input or
output, and the rule. Files are loaded the way the worker loads them (`child._compile`), so
no worker process is needed; a few tests at the end go through the real one
(tests/test_custom_indicators.py).
"""
# ruff: noqa: E501 (the tables of broken files read best one per line)

import textwrap

import pytest

from openchart.indicators._base import SchemaError
from openchart.indicators._custom import indicator_from_json
from openchart.indicators._registry import BUILTIN_DIR
from openchart.indicators.worker.child import _compile

FILE = "mine.py"
PARAMS = """
    Int("length", 5, min=2, max=50),
    Float("level", 70.0, min=0, max=100),
    Source(),
    Color("color", "#ff0000"),
    Color("color2", "#00ff00"),
    Width("width", 2),
"""
OUTPUTS = 'Line("v", "V", color="color", width="width")'


def make(params: str = PARAMS, outputs: str = OUTPUTS, header: str = "", name: str = "'Mine'"):
    return "\n".join(
        [
            "from openchart.indicators import *",
            header,
            f"NAME = {name}",
            'TITLE = "Mine"',
            "OVERLAY = False",
            f"PARAMS = [{textwrap.dedent(params)}]",
            f"OUTPUTS = [{textwrap.dedent(outputs)}]",
            "def compute(bars, params):",
            "    return {}",
            "",
        ]
    )


def load(source: str, file: str = FILE):
    return _compile(file, source)


def fails(source: str, *fragments: str, file: str = FILE) -> str:
    with pytest.raises(SchemaError) as caught:
        load(source, file)
    message = str(caught.value)
    assert message.startswith(f"{file}: "), message
    for fragment in fragments:
        assert fragment in message, message
    return message


# Inputs ----------------------------------------------------------------------------------


def test_the_new_string_input_loads_and_checks_its_value():
    ind = load(make(PARAMS + 'String("tag", "abc", max_length=5, label="Tag"),'))
    tag = next(p for p in ind.params if p.name == "tag")
    assert tag.to_json() == {
        "name": "tag",
        "type": "string",
        "default": "abc",
        "label": "Tag",
        "group": "inputs",
        "max_length": 5,
    }
    assert ind.normalize({"tag": "12345"})["tag"] == "12345"
    with pytest.raises(ValueError, match="at most 5 characters"):
        ind.normalize({"tag": "123456"})
    with pytest.raises(ValueError, match="must be text"):
        ind.normalize({"tag": 7})


def test_string_max_length_defaults_to_100():
    ind = load(make(PARAMS + 'String("tag", ""),'))
    assert next(p for p in ind.params if p.name == "tag").max_length == 100


@pytest.mark.parametrize(
    ("what", "params", "fragments"),
    [
        (
            "a duplicate name",
            'Int("a", 1), Int("a", 2), Color("color", "#fff000"), Width("width")',
            ("input 'a'", "used twice"),
        ),
        (
            "a name that is not an identifier",
            'Int("my-input", 1), Color("color", "#fff000"), Width("width")',
            ("input 'my-input'", "identifier"),
        ),
        (
            "an Int default above its max",
            'Int("length", 99, min=2, max=50), Color("color", "#fff000"), Width("width")',
            ("input 'length'", "default", "at most 50"),
        ),
        (
            "an Int default below its min",
            'Int("length", 1, min=2), Color("color", "#fff000"), Width("width")',
            ("input 'length'", "default", "at least 2"),
        ),
        (
            "an Int default that is not whole",
            'Int("length", 2.5), Color("color", "#fff000"), Width("width")',
            ("input 'length'", "default", "whole number"),
        ),
        (
            "a Float default that is text",
            'Float("level", "70"), Color("color", "#fff000"), Width("width")',
            ("input 'level'", "default", "number"),
        ),
        (
            "a Bool default that is a number",
            'Bool("flag", 1), Color("color", "#fff000"), Width("width")',
            ("input 'flag'", "default", "true or false"),
        ),
        (
            "min above max",
            'Int("length", 5, min=10, max=2), Color("color", "#fff000"), Width("width")',
            ("input 'length'", "min 10", "greater than max 2"),
        ),
        (
            "a Float min above max",
            'Float("x", 1.0, min=3.0, max=2.0), Color("color", "#fff000"), Width("width")',
            ("input 'x'", "min 3", "greater than max 2"),
        ),
        (
            "a step of zero",
            'Float("x", 1.0, step=0), Color("color", "#fff000"), Width("width")',
            ("input 'x'", "step"),
        ),
        (
            "no choices",
            'Choice("mode", "a", []), Color("color", "#fff000"), Width("width")',
            ("input 'mode'", "at least one choice"),
        ),
        (
            "a choice default that is not a choice",
            'Choice("mode", "c", ["a", "b"]), Color("color", "#fff000"), Width("width")',
            ("input 'mode'", "default", "must be one of"),
        ),
        (
            "repeated choices",
            'Choice("mode", "a", ["a", "a"]), Color("color", "#fff000"), Width("width")',
            ("input 'mode'", "choices", "repeated"),
        ),
        (
            "choices that are not text",
            'Choice("mode", "a", ["a", 2]), Color("color", "#fff000"), Width("width")',
            ("input 'mode'", "choices", "text"),
        ),
        (
            "a Color default that is not a colour",
            'Color("color", "red"), Width("width")',
            ("input 'color'", "default", "colour like #2962ff"),
        ),
        (
            "a Width default of 9",
            'Color("color", "#fff000"), Width("width", 9)',
            ("input 'width'", "default", "at most 4"),
        ),
        (
            "a Source default that is not a source",
            'Source("source", "volume"), Color("color", "#fff000"), Width("width")',
            ("input 'source'", "default", "must be one of"),
        ),
        (
            "a String default longer than its max_length",
            'String("tag", "abcdef", max_length=3), Color("color", "#fff000"), Width("width")',
            ("input 'tag'", "default", "at most 3 characters"),
        ),
        (
            "a String default that is not text",
            'String("tag", 5), Color("color", "#fff000"), Width("width")',
            ("input 'tag'", "default", "text"),
        ),
        (
            "a String max_length of 0",
            'String("tag", "", max_length=0), Color("color", "#fff000"), Width("width")',
            ("input 'tag'", "max_length", "between 1 and 1000"),
        ),
        (
            "a String max_length of 5000",
            'String("tag", "", max_length=5000), Color("color", "#fff000"), Width("width")',
            ("input 'tag'", "max_length", "between 1 and 1000"),
        ),
        (
            "a label of 61 characters",
            'Int("length", 5, label="x" * 61), Color("color", "#fff000"), Width("width")',
            ("input 'length'", "label", "at most 60 characters"),
        ),
        (
            "something that is not an input",
            '"length", Color("color", "#fff000"), Width("width")',
            ("PARAMS must hold",),
        ),
    ],
)
def test_input_rules(what, params, fragments):
    fails(make(params), *fragments)


def test_a_label_of_exactly_60_characters_is_fine():
    load(make(PARAMS + 'Int("n", 1, label="x" * 60),'))


def test_30_inputs_load_and_31_do_not():
    many = lambda n: ", ".join(f'Int("i{k}", 1)' for k in range(n))  # noqa: E731
    load(make(many(28) + ', Color("color", "#fff000"), Width("width")'))
    fails(
        make(many(29) + ', Color("color", "#fff000"), Width("width")'),
        "31 inputs",
        "at most 30",
    )


# Outputs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("what", "outputs", "fragments"),
    [
        (
            "a duplicate name",
            'Line("v", "A", color="color", width="width"), Histogram("v", "B", color="color")',
            ("output 'v'", "used twice"),
        ),
        (
            "a name that is not an identifier",
            'Line("my v", "A", color="color", width="width")',
            ("output 'my v'", "identifier"),
        ),
        (
            "a colour that is no input",
            'Line("v", "A", color="nope", width="width")',
            ("output 'v'", "'nope' is not a color parameter"),
        ),
        (
            "a colour that is the wrong type",
            'Line("v", "A", color="length", width="width")',
            ("output 'v'", "'length' is not a color parameter"),
        ),
        (
            "a width that is no input",
            'Line("v", "A", color="color", width="nope")',
            ("output 'v'", "'nope' is not a width parameter"),
        ),
        (
            "a width that is the wrong type",
            'Line("v", "A", color="color", width="color")',
            ("output 'v'", "'color' is not a width parameter"),
        ),
        (
            "a style that is unknown",
            'Line("v", "A", color="color", width="width", style="wavy")',
            ("output 'v'", "style", "solid, dashed or dotted"),
        ),
        (
            "a label of 61 characters",
            'Line("v", "x" * 61, color="color", width="width")',
            ("output 'v'", "label", "at most 60 characters"),
        ),
        (
            "a Histogram with no colour input",
            'Histogram("h", "H", color="width")',
            ("output 'h'", "'width' is not a color parameter"),
        ),
        (
            "a Histogram base that is text",
            'Histogram("h", "H", color="color", base="0")',
            ("output 'h'", "base", "number"),
        ),
        (
            "an Area with a bad width input",
            'Area("a", "A", color="color", width="color")',
            ("output 'a'", "'color' is not a width parameter"),
        ),
        (
            "a Fill of one output",
            'Line("v", "A", color="color", width="width"), Fill(between=("v",), color="color2")',
            ("Fill", "two output names"),
        ),
        (
            "a Fill of an output that does not exist",
            'Line("v", "A", color="color", width="width"), Fill(between=("v", "w"), color="color2")',
            ("Fill", "'w'", "no such output"),
        ),
        (
            "a Fill of a Histogram",
            'Line("v", "A", color="color", width="width"), Histogram("h", "H", color="color"), Fill(between=("v", "h"), color="color2")',
            ("Fill", "'h'", "line or area"),
        ),
        (
            "a Fill of one output twice",
            'Line("v", "A", color="color", width="width"), Fill(between=("v", "v"), color="color2")',
            ("Fill", "two different outputs"),
        ),
        (
            "a Fill of a Marker",
            'Line("v", "A", color="color", width="width"), Marker("m", "M", color="color"), Fill(between=("v", "m"), color="color2")',
            ("Fill", "'m'", "line or area"),
        ),
        (
            "a Fill with a colour that is no colour input",
            'Line("v", "A", color="color", width="width"), Line("w", "B", color="color2", width="width"), Fill(between=("v", "w"), color="length")',
            ("Fill", "'length' is not a color parameter"),
        ),
        (
            "a Fill opacity of 0",
            'Line("v", "A", color="color", width="width"), Line("w", "B", color="color2", width="width"), Fill(between=("v", "w"), color="color2", opacity=0)',
            ("Fill", "opacity", "above 0 and at most 1"),
        ),
        (
            "a Fill opacity above 1",
            'Line("v", "A", color="color", width="width"), Line("w", "B", color="color2", width="width"), Fill(between=("v", "w"), color="color2", opacity=2)',
            ("Fill", "opacity", "above 0 and at most 1"),
        ),
        (
            "a Level naming no input",
            'Line("v", "A", color="color", width="width"), Level("nope", "L", color="color2")',
            ("Level", "'nope'", "no such input"),
        ),
        (
            "a Level naming an Int input",
            'Line("v", "A", color="color", width="width"), Level("length", "L", color="color2")',
            ("Level", "'length'", "not a Float input"),
        ),
        (
            "a Level value that is not finite",
            'Line("v", "A", color="color", width="width"), Level(float("nan"), "L", color="color2")',
            ("Level", "finite number"),
        ),
        (
            "a Level value that is true",
            'Line("v", "A", color="color", width="width"), Level(True, "L", color="color2")',
            ("Level", "number or the name of a Float input"),
        ),
        (
            "a Level with a bad style",
            'Line("v", "A", color="color", width="width"), Level(1.0, "L", color="color2", style="x")',
            ("Level", "style", "solid, dashed or dotted"),
        ),
        (
            "a Level with a bad width input",
            'Line("v", "A", color="color", width="width"), Level(1.0, "L", color="color2", width="color")',
            ("Level", "'color' is not a width parameter"),
        ),
        (
            "a Marker shape that is unknown",
            'Marker("m", "M", color="color", shape="star")',
            ("output 'm'", "shape", "arrow_up, arrow_down, circle or square"),
        ),
        (
            "a Marker position that is unknown",
            'Marker("m", "M", color="color", position="left")',
            ("output 'm'", "position", "above, below or on"),
        ),
        (
            "a Marker text of 61 characters",
            'Marker("m", "M", color="color", text="x" * 61)',
            ("output 'm'", "text", "at most 60 characters"),
        ),
        (
            "a Background opacity of 0",
            'Background("b", "B", color="color", opacity=0)',
            ("output 'b'", "opacity", "above 0 and at most 1"),
        ),
        (
            "a Background with a bad colour",
            'Background("b", "B", color="width")',
            ("output 'b'", "'width' is not a color parameter"),
        ),
        ("no outputs", "", ("OUTPUTS must hold at least one",)),
        ("something that is not an output", '"v"', ("OUTPUTS must hold",)),
    ],
)
def test_output_rules(what, outputs, fragments):
    fails(make(outputs=outputs), *fragments)


def test_16_outputs_load_and_17_do_not():
    many = lambda n: ", ".join(  # noqa: E731
        f'Line("v{k}", "V", color="color", width="width")' for k in range(n)
    )
    load(make(outputs=many(16)))
    fails(make(outputs=many(17)), "17 outputs", "at most 16")


def test_names_in_inputs_and_in_outputs_may_be_the_same():
    # The design's own example has an input "fast" and an output "fast".
    load(make(PARAMS + 'Int("v", 3),'))


def test_a_file_larger_than_256_kb_is_refused_before_it_runs():
    ran = "RAN = 1\n"
    fails(ran + "#" * (256 * 1024), "larger than 256 KB")


def test_name_and_title_are_at_most_60_characters():
    fails(make(name='"x" * 61'), "NAME", "at most 60 characters")


def test_the_message_names_the_file_it_was_given():
    fails(
        make(outputs='Line("v", "A", color="nope", width="width")'),
        "other_name.py",
        file="other_name.py",
    )


# What loads ------------------------------------------------------------------------------

ALL_KINDS = """
    Line("fast", "Fast", color="color", width="width", style="dotted"),
    Line("slow", "Slow", color="color2", width="width"),
    Histogram("hist", "Hist", color="color", base=1.5),
    Area("area", "Area", color="color2"),
    Area("area2", "Area 2", color="color2", width="width"),
    Fill(between=("fast", "slow"), color="color2", opacity=0.3),
    Fill(between=("area", "fast"), color="color"),
    Level(70, "Upper", color="color"),
    Level("level", "Input level", color="color2", width="width", style="solid"),
    Marker("up", "Up", color="color", shape="arrow_up", position="below", text="buy"),
    Marker("dot", "Dot", color="color2"),
    Background("bg", "Bg", color="color2", opacity=0.4),
"""


def test_every_output_kind_loads_and_describes_itself_for_the_browser():
    ind = load(make(outputs=ALL_KINDS))
    got = [o.to_json(i) for o, i in zip(ind.outputs, ind.output_ids(), strict=True)]
    assert got == [
        {"kind": "line", "name": "fast", "label": "Fast", "color": "color", "width": "width", "style": "dotted"},
        {"kind": "line", "name": "slow", "label": "Slow", "color": "color2", "width": "width", "style": "solid"},
        {"kind": "histogram", "name": "hist", "label": "Hist", "color": "color", "base": 1.5},
        {"kind": "area", "name": "area", "label": "Area", "color": "color2", "width": None},
        {"kind": "area", "name": "area2", "label": "Area 2", "color": "color2", "width": "width"},
        {"kind": "fill", "name": "fill:1", "label": "Fill", "between": ["fast", "slow"], "color": "color2", "opacity": 0.3},
        {"kind": "fill", "name": "fill:2", "label": "Fill", "between": ["area", "fast"], "color": "color", "opacity": 0.15},
        {"kind": "level", "name": "level:1", "label": "Upper", "value": 70.0, "input": None, "color": "color", "width": None, "style": "dashed"},
        {"kind": "level", "name": "level:2", "label": "Input level", "value": None, "input": "level", "color": "color2", "width": "width", "style": "solid"},
        {"kind": "marker", "name": "up", "label": "Up", "color": "color", "shape": "arrow_up", "position": "below", "text": "buy"},
        {"kind": "marker", "name": "dot", "label": "Dot", "color": "color2", "shape": "circle", "position": "above", "text": ""},
        {"kind": "background", "name": "bg", "label": "Bg", "color": "color2", "opacity": 0.4},
    ]  # fmt: skip


def test_what_the_worker_describes_is_checked_again_by_the_server():
    ind = load(make(outputs=ALL_KINDS))
    wire = ind.to_json()
    again = indicator_from_json("mine", wire, file="mine.py")
    assert again.to_json() == {**wire, "id": "mine", "builtin": False}
    # A worker that lies about its schema is not believed (the same rules apply).
    bad = {**wire, "outputs": [{**wire["outputs"][0], "color": "length"}]}
    with pytest.raises(SchemaError, match="'length' is not a color parameter"):
        indicator_from_json("mine", bad, file="mine.py")
    for broken in (
        {**wire, "outputs": wire["outputs"] + [{"kind": "sparkle", "name": "x", "label": "X"}]},
        {**wire, "outputs": [{"kind": "line", "name": "x"}]},
        {**wire, "outputs": "line"},
        {**wire, "params": [{"name": "p", "type": "quaternion", "default": 1}]},
    ):
        with pytest.raises(ValueError):
            indicator_from_json("mine", broken, file="mine.py")


# Compatibility ---------------------------------------------------------------------------

M7_FILE = '''
"""An M7 file: Output(...) positional, no kinds, compute with two arguments."""
from openchart.indicators import Color, Int, Output, Source, Width, sma, source

NAME = "Mid"
TITLE = "Middle line"
OVERLAY = False
PARAMS = [Int("length", 5, min=2), Source(), Color("color", "#ff0000"), Width("width", 2)]
OUTPUTS = [Output("mid", "Mid", color="color", width="width"), Output("two", "Two", "color", "width")]

def compute(bars, params):
    return {"mid": sma(source(bars, params["source"]), params["length"]), "two": bars.c}
'''


def test_an_m7_format_file_loads_unchanged():
    ind = load(M7_FILE, "mid.py")
    assert [(o.kind, o.name, o.color, o.width, o.style) for o in ind.outputs] == [
        ("line", "mid", "color", "width", "solid"),
        ("line", "two", "color", "width", "solid"),
    ]
    # The M7 fields are all still there, in the same places.
    assert ind.to_json()["outputs"][0] == {
        "kind": "line",
        "name": "mid",
        "label": "Mid",
        "color": "color",
        "width": "width",
        "style": "solid",
    }


def test_output_is_an_alias_of_line():
    from openchart.indicators import Line, Output

    assert Output is Line


def test_the_builtin_ema_loads_unchanged():
    ind = load((BUILTIN_DIR / "ema.py").read_text(encoding="utf-8"), "ema.py")
    assert ind.to_json()["outputs"] == [
        {
            "kind": "line",
            "name": "ema",
            "label": "EMA",
            "color": "color",
            "width": "width",
            "style": "solid",
        }
    ]
    assert [p.name for p in ind.params] == ["length", "source", "color", "width"]
