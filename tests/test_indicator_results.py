"""What `compute` returns is checked before anything is drawn (CI-2 step b, point 4), and
what `compute` receives: the long column names and the optional `info`.

`Indicator.run` is the script-side check (types), `Indicator.check_wire` the server-side
one on the arrays the worker sent back, which are never trusted.
"""
# ruff: noqa: E501 (the tables of broken files read best one per line)

import math
import textwrap

import numpy as np
import pandas as pd
import pytest

from openchart.indicators._base import Info, ResultError
from openchart.indicators.worker.child import _compile

BARS = pd.DataFrame(
    {
        "ts": [1000.0 + 60 * i for i in range(6)],
        "o": [1.0, 2, 3, 4, 5, 6],
        "h": [2.0, 3, 4, 5, 6, 7],
        "l": [0.5, 1, 2, 3, 4, 5],
        "c": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
        "v": [10.0, 20, 30, 40, 50, 60],
    }
)
N = len(BARS)


def indicator(body: str, outputs: str | None = None, signature: str = "bars, params"):
    outputs = outputs or (
        'Line("v", "V", color="color", width="width"), Marker("m", "M", color="color")'
    )
    return _compile(
        "r.py",
        textwrap.dedent(
            f"""
            from openchart.indicators import *
            NAME = "R"
            OVERLAY = False
            PARAMS = [Color("color", "#ff0000"), Width("width", 1)]
            OUTPUTS = [{outputs}]
            def compute({signature}):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    )


def run(body: str, **kw):
    return indicator(body, **kw).run(BARS, {"color": "#ff0000", "width": 1})


def refused(body: str, *fragments: str, **kw):
    with pytest.raises(ResultError) as caught:
        run(body, **kw)
    for fragment in fragments:
        assert fragment in str(caught.value), str(caught.value)


GOOD_V = "[1.0, 2, 3, 4, 5, 6]"
GOOD_M = "[False, True, False, False, True, False]"


def test_a_good_result_is_floats_for_numbers_and_zero_one_for_booleans():
    out = run(f"return {{'v': {GOOD_V}, 'm': {GOOD_M}}}")
    assert list(out) == ["v", "m"]
    assert out["v"].tolist() == [1, 2, 3, 4, 5, 6]
    assert out["m"].tolist() == [0, 1, 0, 0, 1, 0]
    assert all(s.dtype == "float64" for s in out.values())


@pytest.mark.parametrize(
    "value",
    [
        "pd.Series([1, 2, 3, 4, 5, 6])",
        "np.arange(6)",
        "np.arange(6, dtype='float32')",
        "(1, 2, 3, 4, 5, 6)",
        "[1, None, 3, 4, 5, 6]",
        "pd.Series([1.0, None, 3, 4, 5, 6], dtype='Float64')",
        "pd.Series([1, 2, 3, 4, 5, 6], index=list('abcdef'))",
    ],
)
def test_numbers_may_come_as_series_arrays_lists_and_tuples(value):
    out = run(f"import numpy as np, pandas as pd\nreturn {{'v': {value}, 'm': {GOOD_M}}}")
    assert len(out["v"]) == N and out["v"].dtype == "float64"


@pytest.mark.parametrize(
    "value",
    [
        "pd.Series([True, False, True, False, True, False])",
        "np.array([True, False, True, False, True, False])",
        "[True, False, True, False, True, False]",
        "ta.crossover(bars.c, 3)",
    ],
)
def test_booleans_may_come_as_series_arrays_and_lists(value):
    out = run(f"import numpy as np, pandas as pd\nreturn {{'v': {GOOD_V}, 'm': {value}}}")
    assert set(out["m"].tolist()) <= {0.0, 1.0}


def test_gaps_and_infinities_are_gaps_in_a_line():
    out = run(
        f"return {{'v': [1.0, float('nan'), float('inf'), -float('inf'), 5, 6], 'm': {GOOD_M}}}"
    )
    assert out["v"].isna().tolist() == [False, True, True, True, False, False]


def test_a_line_of_nothing_but_gaps_is_allowed_and_harmless():
    # Indistinguishable from "not enough bars yet" (an SMA(200) on 100 bars), so it is
    # not an error: it draws nothing.
    out = run(f"return {{'v': [float('nan')] * 6, 'm': {GOOD_M}}}")
    assert out["v"].isna().all()


@pytest.mark.parametrize(
    ("what", "body", "fragments"),
    [
        ("a list instead of a dict", "return [1, 2, 3]", ("dict", "output name")),
        ("nothing", "return None", ("dict",)),
        ("a DataFrame", "return pd.DataFrame({'v': [1] * 6})", ("dict",)),
        ("a missing output", f"return {{'v': {GOOD_V}}}", ("no 'm' output",)),
        (
            "an undeclared output",
            f"return {{'v': {GOOD_V}, 'm': {GOOD_M}, 'extra': {GOOD_V}}}",
            ("'extra'", "not a declared output"),
        ),
        (
            "an output name that is not text",
            f"return {{'v': {GOOD_V}, 'm': {GOOD_M}, 3: {GOOD_V}}}",
            ("3", "not a declared output"),
        ),
        (
            "too few values",
            f"return {{'v': [1.0, 2.0], 'm': {GOOD_M}}}",
            ("'v'", "2 values for 6 bars"),
        ),
        (
            "too many values",
            f"return {{'v': [1.0] * 7, 'm': {GOOD_M}}}",
            ("'v'", "7 values for 6 bars"),
        ),
        (
            "a million values",
            f"return {{'v': [1.0] * 1_000_000, 'm': {GOOD_M}}}",
            ("'v'", "1,000,000 values for 6 bars"),
        ),
        (
            "one number",
            f"return {{'v': 3.0, 'm': {GOOD_M}}}",
            ("'v'", "one value per bar", "single value"),
        ),
        ("text", f"return {{'v': 'abcdef', 'm': {GOOD_M}}}", ("'v'", "series")),
        ("a generator", f"return {{'v': (x for x in range(6)), 'm': {GOOD_M}}}", ("'v'", "series")),
        (
            "a dict as values",
            f"return {{'v': {{'a': 1}}, 'm': {GOOD_M}}}",
            ("'v'", "series"),
        ),
        (
            "text in a line",
            f"return {{'v': ['a', 'b', 'c', 'd', 'e', 'f'], 'm': {GOOD_M}}}",
            ("'v'", "numbers"),
        ),
        (
            "text mixed into numbers",
            f"return {{'v': [1, 2, 'x', 4, 5, 6], 'm': {GOOD_M}}}",
            ("'v'", "numbers"),
        ),
        (
            "booleans in a line",
            f"return {{'v': [True, False, True, False, True, False], 'm': {GOOD_M}}}",
            ("'v'", "numbers", "true/false"),
        ),
        (
            "a two-dimensional array",
            f"return {{'v': np.zeros((6, 2)), 'm': {GOOD_M}}}",
            ("'v'", "one-dimensional"),
        ),
        (
            "numbers in a marker",
            f"return {{'v': {GOOD_V}, 'm': [0, 1, 0, 0, 1, 0]}}",
            ("'m'", "true/false", "numbers"),
        ),
        (
            "floats with gaps in a marker",
            f"return {{'v': {GOOD_V}, 'm': [1.0, float('nan'), 0, 0, 1, 0]}}",
            ("'m'", "true/false"),
        ),
        (
            "None in a marker",
            f"return {{'v': {GOOD_V}, 'm': [True, None, False, False, True, False]}}",
            ("'m'", "true/false"),
        ),
        (
            "text in a marker",
            f"return {{'v': {GOOD_V}, 'm': ['a'] * 6}}",
            ("'m'", "true/false"),
        ),
    ],
)
def test_bad_results_are_refused_with_a_message_naming_the_output(what, body, fragments):
    refused("import numpy as np, pandas as pd\n" + body, *fragments)


def test_fill_and_level_take_no_values():
    outputs = (
        'Line("a", "A", color="color", width="width"), Line("b", "B", color="color", width="width"),'
        ' Fill(between=("a", "b"), color="color"), Level(1.0, "L", color="color")'
    )
    out = run("return {'a': [1] * 6, 'b': [2] * 6}", outputs=outputs)
    assert list(out) == ["a", "b"]
    refused(
        "return {'a': [1] * 6, 'b': [2] * 6, 'fill:1': [1] * 6}",
        "'fill:1'",
        "takes no values",
        outputs=outputs,
    )
    refused(
        "return {'a': [1] * 6, 'b': [2] * 6, 'level:1': [1] * 6}",
        "'level:1'",
        "takes no values",
        outputs=outputs,
    )


def test_histogram_and_area_take_numbers_like_a_line():
    outputs = 'Histogram("h", "H", color="color"), Area("a", "A", color="color")'
    out = run("return {'h': [1] * 6, 'a': [2] * 6}", outputs=outputs)
    assert out["h"].tolist() == [1] * 6
    refused(
        "return {'h': [True] * 6, 'a': [2] * 6}",
        "'h'",
        "numbers",
        outputs=outputs,
    )
    refused("return {'h': [1] * 6, 'a': [1] * 5}", "'a'", "5 values for 6 bars", outputs=outputs)


def test_background_takes_booleans():
    outputs = 'Background("b", "B", color="color")'
    assert run("return {'b': [True, False] * 3}", outputs=outputs)["b"].sum() == 3
    refused("return {'b': [1, 0] * 3}", "'b'", "true/false", outputs=outputs)


def test_an_exception_in_compute_is_the_scripts_own_and_passes_through():
    with pytest.raises(ZeroDivisionError):
        run("return {'v': 1 / 0}")


# Wire check: what the server accepts from the worker ----------------------------------------


def wire(**arrays):
    return {k: np.asarray(v, dtype="float64") for k, v in arrays.items()}


def test_the_server_check_accepts_what_the_worker_should_send():
    ind = indicator("return {}")
    out = ind.check_wire(wire(v=[1, 2, 3, 4, 5, math.nan], m=[0, 1, 0, 0, 1, 0]), N)
    assert list(out) == ["v", "m"]
    assert out["v"].isna().tolist() == [False] * 5 + [True]
    assert out["m"].tolist() == [0, 1, 0, 0, 1, 0]


@pytest.mark.parametrize(
    ("what", "arrays", "fragments"),
    [
        ("a missing output", {"v": [1] * 6}, ("no 'm' output",)),
        (
            "an extra output",
            {"v": [1] * 6, "m": [0] * 6, "zzz": [1] * 6},
            ("'zzz'", "not a declared output"),
        ),
        ("a short array", {"v": [1] * 5, "m": [0] * 6}, ("'v'", "5 values for 6 bars")),
        ("a long array", {"v": [1] * 6, "m": [0] * 7}, ("'m'", "7 values for 6 bars")),
        ("a marker of 2", {"v": [1] * 6, "m": [0, 2, 0, 0, 0, 0]}, ("'m'", "true/false")),
        ("a marker of 0.5", {"v": [1] * 6, "m": [0, 0.5, 0, 0, 0, 0]}, ("'m'", "true/false")),
        ("a marker of NaN", {"v": [1] * 6, "m": [0, math.nan, 0, 0, 0, 0]}, ("'m'", "true/false")),
        (
            "a marker of infinity",
            {"v": [1] * 6, "m": [0, math.inf, 0, 0, 0, 0]},
            ("'m'", "true/false"),
        ),
    ],
)
def test_the_server_check_refuses_what_it_cannot_believe(what, arrays, fragments):
    ind = indicator("return {}")
    with pytest.raises(ResultError) as caught:
        ind.check_wire(wire(**arrays), N)
    for fragment in fragments:
        assert fragment in str(caught.value), str(caught.value)


def test_the_server_check_turns_infinities_in_numbers_into_gaps():
    out = indicator("return {}").check_wire(wire(v=[1, math.inf, -math.inf, 4, 5, 6], m=[0] * 6), N)
    assert out["v"].isna().tolist() == [False, True, True, False, False, False]


def test_the_server_check_refuses_arrays_that_are_not_float64_vectors():
    ind = indicator("return {}")
    with pytest.raises(ResultError, match="'v'"):
        ind.check_wire({"v": np.zeros((6, 2)), "m": np.zeros(6)}, N)
    with pytest.raises(ResultError, match="'v'"):
        ind.check_wire({"v": np.arange(6, dtype="int64"), "m": np.zeros(6)}, N)


# What compute receives ----------------------------------------------------------------------


def test_bars_answer_to_the_long_names():
    out = run(
        """
        assert bars.close.tolist() == bars["c"].tolist() == bars["close"].tolist()
        assert bars.open.tolist() == bars.o.tolist()
        assert bars.high.tolist() == bars.h.tolist()
        assert bars.low.tolist() == bars.l.tolist()
        assert bars.volume.tolist() == bars.v.tolist()
        assert bars.time.tolist() == bars.ts.tolist()
        return {'v': bars.close, 'm': bars.close > 3}
        """
    )
    assert out["v"].tolist() == [1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
    assert out["m"].tolist() == [0, 0, 1, 1, 1, 1]


def test_the_caller_keeps_its_own_frame_unchanged():
    before = list(BARS.columns)
    run(f"return {{'v': {GOOD_V}, 'm': {GOOD_M}}}")
    assert list(BARS.columns) == before


INFO = Info(instrument="XAU_USD", display_name="Gold", decimals=2, timeframe="1h", price="mid")


def test_info_is_passed_only_to_a_compute_that_declares_a_third_argument():
    body = f"return {{'v': [info.decimals] * 6, 'm': {GOOD_M}}}"
    ind = indicator(body, signature="bars, params, info")
    out = ind.run(BARS, {"color": "#ff0000", "width": 1}, INFO)
    assert out["v"].tolist() == [2.0] * 6
    two = indicator(f"return {{'v': {GOOD_V}, 'm': {GOOD_M}}}")
    assert two.run(BARS, {"color": "#ff0000", "width": 1}, INFO)["v"].tolist() == [1, 2, 3, 4, 5, 6]


def test_info_carries_the_five_documented_fields():
    seen = {}
    ind = indicator(
        f"seen.update(vars(info))\nreturn {{'v': {GOOD_V}, 'm': {GOOD_M}}}",
        signature="bars, params, info",
    )
    ind.compute.__globals__["seen"] = seen
    ind.run(BARS, {"color": "#ff0000", "width": 1}, INFO)
    assert seen == {
        "instrument": "XAU_USD",
        "display_name": "Gold",
        "decimals": 2,
        "timeframe": "1h",
        "price": "mid",
    }


def test_info_is_read_only_and_checked():
    with pytest.raises(AttributeError):
        INFO.decimals = 3  # type: ignore[misc]
    assert Info.from_json(INFO.to_json()) == INFO
    for bad in (
        None,
        [],
        {**INFO.to_json(), "extra": 1},
        {k: v for k, v in INFO.to_json().items() if k != "price"},
        {**INFO.to_json(), "decimals": "2"},
        {**INFO.to_json(), "decimals": True},
        {**INFO.to_json(), "instrument": 5},
    ):
        with pytest.raises(ValueError):
            Info.from_json(bad)


def test_a_compute_with_a_default_third_argument_or_star_args_gets_info():
    for signature in ("bars, params, info=None", "bars, params, *rest"):
        body = (
            "x = info.decimals if 'info' in dir() else rest[0].decimals\n"
            f"return {{'v': [x] * 6, 'm': {GOOD_M}}}"
        )
        ind = indicator(body, signature=signature)
        assert ind.run(BARS, {"color": "#ff0000", "width": 1}, INFO)["v"].tolist() == [2.0] * 6
