"""The save-time checks (CI-2 step c): what they flag, what they do not, and why.

The comparison rules run here on scripts compiled the way the worker compiles them, run
in this test process on synthetic bars (the server never runs them: that is the worker's
job, tests/test_check_scheduling.py). Each run below is what the worker would send back,
checked by `Indicator.check_wire` as the server checks it.
"""

import math
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from openchart.indicators import _checks
from openchart.indicators._registry import BUILTIN_DIR
from openchart.indicators.worker.child import _compile
from tests.test_custom_indicator_kinds import CROSS
from tests.test_custom_indicators import GOOD

HEAD = """
import numpy as np
import pandas as pd
from openchart.indicators import (
    Background, Color, Float, Int, Line, Marker, Width, ema, sma, source, ta,
)

NAME = "T"
TITLE = "Test"
OVERLAY = True
PARAMS = [Color("color", "#2962ff"), Width("width", 1){params}]
OUTPUTS = [{outputs}]


def warmup(params):
    return {warmup}


def compute(bars, params):
    c, h, l, v = bars.c, bars.h, bars.l, bars.v
{body}
"""
LINE = 'Line("x", "X", color="color", width="width")'


def make(body, *, warmup=0, outputs=LINE, params=""):
    text = HEAD.format(
        params=params,
        outputs=outputs,
        warmup=warmup,
        body=textwrap.indent(textwrap.dedent(body).strip(), "    "),
    )
    return _compile("t.py", text)


def make_bars(n=400, seed=7, start=2000.0):
    rng = np.random.default_rng(seed)
    c = (start + np.cumsum(rng.normal(0, 0.8, n))).round(3)
    o = np.concatenate([[start], c[:-1]])
    h = (np.maximum(o, c) + rng.uniform(0, 0.6, n)).round(3)
    low = (np.minimum(o, c) - rng.uniform(0, 0.6, n)).round(3)
    v = rng.integers(1, 200, n).astype(float)
    ts = 1_760_450_400.0 + 300 * np.arange(n)
    return pd.DataFrame({"ts": ts, "o": o, "h": h, "l": low, "c": c, "v": v})


def run(indicator, frame, params, info=None):
    """One run as the server receives it from the worker."""
    frame = frame.reset_index(drop=True)
    series = indicator.run(frame, params, info)
    arrays = {k: s.to_numpy(dtype="float64") for k, s in series.items()}
    return {k: s.to_numpy() for k, s in indicator.check_wire(arrays, len(frame)).items()}


def check(indicator, frame, params=None, info=None):
    """Both checks, as the server would do them: (plan, lookahead findings, warmup findings)."""
    p = indicator.normalize(params or {})
    plan = _checks.plan(len(frame), int(indicator.warmup(p)))
    full = run(indicator, frame, p, info)
    t = frame["ts"].to_numpy()
    ahead = behind = []
    if plan.cut:
        part = run(indicator, frame.iloc[: len(frame) - plan.cut], p, info)
        ahead = _checks.lookahead_findings(indicator, full, part, t)
    if plan.start is not None:
        offset = run(indicator, frame.iloc[plan.start :], p, info)
        behind = _checks.warmup_findings(indicator, full, offset, plan, t)
    return plan, ahead, behind


def warnings(findings):
    return [f for f in findings if f["level"] == "warning"]


# Lookahead that must be flagged -------------------------------------------------------

LOOKAHEAD = {
    "shift(-1)": "return {'x': c.shift(-1)}",
    "shift(-60), further than the 50 bars removed": "return {'x': c.shift(-60)}",
    "a centred window": "return {'x': c.rolling(5, center=True).mean()}",
    "a loop reading bars[i + 1]": """
        a = c.to_numpy()
        out = np.full(len(a), np.nan)
        for i in range(len(a) - 1):
            out[i] = a[i + 1] - a[i]
        return {'x': out}
    """,
    "the mean of the whole series": "return {'x': c - c.mean()}",
    "a z-score with the whole series' deviation": "return {'x': (c - c.mean()) / c.std()}",
    "the last bar, everywhere": "return {'x': c * 0 + c.iloc[-1]}",
    "a branch on the bar count, inside the window": "return {'x': c if len(c) > 390 else c * 2}",
    "bfill of a gap that ends in the last bars": """
        x = c.copy()
        x.iloc[320:370] = np.nan  # filled from bar 370, which the shorter run lacks
        return {'x': x.bfill()}
    """,
}


@pytest.mark.parametrize("body", LOOKAHEAD.values(), ids=list(LOOKAHEAD))
def test_lookahead_is_flagged(body):
    frame = make_bars(400)
    plan, ahead, _ = check(make(body), frame)
    assert plan.cut == 50
    found = warnings(ahead)
    assert [f["output"] for f in found] == ["x"]
    f = found[0]
    assert f["check"] == "lookahead"
    assert f["t"] in frame["ts"].tolist()
    assert f["full"] != f["other"]
    assert "later bars" in f["message"]


def test_the_finding_is_the_first_bar_that_differs_with_both_values():
    frame = make_bars(400)
    _, ahead, _ = check(make("return {'x': c.shift(-1)}"), frame)
    (f,) = ahead
    # With 350 bars the last value of shift(-1) is a gap; with 400 it is bar 350's close.
    assert f["t"] == frame["ts"][349]
    assert f["full"] == frame["c"][350]
    assert f["other"] is None
    assert "with all 400 bars" in f["message"] and "with 350" in f["message"]


def test_of_several_differing_bars_the_first_is_reported():
    frame = make_bars(400)
    _, ahead, _ = check(make("return {'x': c.shift(-3)}"), frame)
    (f,) = ahead
    assert f["t"] == frame["ts"][347]  # bars 347, 348 and 349 differ


def test_lookahead_in_a_marker_and_a_background_is_flagged():
    outputs = LINE + ', Marker("up", "Up", color="color"), Background("bg", "Bg", color="color")'
    # True wherever a later bar exists. A true/false output is only seen to look ahead where
    # it flips: `c.shift(-1) > c` would pass on data where the removed close is lower.
    body = "return {'x': c, 'up': c.shift(-1) > c - 1000, 'bg': c.shift(-2) < c + 1000}"
    _, ahead, _ = check(make(body, outputs=outputs), make_bars(400))
    found = {f["output"]: f for f in warnings(ahead)}
    assert set(found) == {"up", "bg"}
    assert {found["up"]["full"], found["up"]["other"]} == {0.0, 1.0}


def test_random_output_is_flagged_with_the_second_reason():
    _, ahead, _ = check(make("return {'x': c + np.random.random(len(c))}"), make_bars(400))
    (f,) = warnings(ahead)
    assert "the same result twice" in f["message"]


# Clean scripts that must not be flagged ------------------------------------------------

DESIGN_SMA_CROSS = '''
"""SMA cross: two moving averages and a marker where they cross (the design's example)."""

from openchart.indicators import Color, Int, Line, Marker, Source, Width, source, ta

NAME = "SMA x"
TITLE = "SMA cross"
OVERLAY = True

PARAMS = [
    Int("fast", 10, min=1, max=500),
    Int("slow", 30, min=2, max=2000),
    Source("source", "close"),
    Color("fast_color", "#2962ff"),
    Color("slow_color", "#ff6d00"),
    Width("width", 1),
    Color("up_color", "#26a69a"),
    Color("down_color", "#ef5350"),
]
OUTPUTS = [
    Line("fast", "Fast", color="fast_color", width="width"),
    Line("slow", "Slow", color="slow_color", width="width"),
    Marker("up", "Cross up", color="up_color", shape="arrow_up", position="below"),
    Marker("down", "Cross down", color="down_color", shape="arrow_down", position="above"),
]


def warmup(params):
    return params["slow"]


def compute(bars, params, info):
    src = source(bars, params["source"])
    fast, slow = ta.sma(src, params["fast"]), ta.sma(src, params["slow"])
    return {
        "fast": fast,
        "slow": slow,
        "up": ta.crossover(fast, slow),
        "down": ta.crossunder(fast, slow),
    }
'''

EXAMPLES = {
    "the design's SMA cross": DESIGN_SMA_CROSS,
    "every output kind (CI-2 b)": CROSS.replace(
        "def compute", "def warmup(p):\n    return 5\n\ndef compute"
    ),
    "the built-in EMA file": (Path(BUILTIN_DIR) / "ema.py").read_text(encoding="utf-8"),
}


@pytest.mark.parametrize("text", EXAMPLES.values(), ids=list(EXAMPLES))
def test_the_examples_and_the_builtin_are_not_flagged(text):
    indicator = _compile("example.py", textwrap.dedent(text))
    plan, ahead, behind = check(indicator, make_bars(1000))
    assert plan.cut and plan.start is not None
    assert ahead == []
    assert warnings(behind) == []


# Every ta helper, with the warmup docs/indicators.md gives it: (expression, warmup, kind).
L = 14
HELPERS = {
    "sma": ("ta.sma(c, 14)", L, "line"),
    "ema": ("ta.ema(c, 14)", 10 * L + L, "line"),
    "rma": ("ta.rma(c, 14)", 20 * L + L, "line"),
    "wma": ("ta.wma(c, 14)", L, "line"),
    "vwma": ("ta.vwma(c, v, 14)", L, "line"),
    "hma": ("ta.hma(c, 16)", 16 + 4 - 1, "line"),
    "shift": ("ta.shift(c, 3)", 3, "line"),
    "change": ("ta.change(c, 3)", 3, "line"),
    "nz": ("ta.nz(ta.sma(c, 5))", 5, "line"),
    "crossover": ("ta.crossover(ta.sma(c, 5), ta.sma(c, 10))", 11, "marker"),
    "crossunder": ("ta.crossunder(ta.sma(c, 5), ta.sma(c, 10))", 11, "marker"),
    "cross": ("ta.cross(ta.sma(c, 5), ta.sma(c, 10))", 11, "marker"),
    # barssince and valuewhen need `cond` to have been True once (n + 1 times) in the
    # warmup: c > c[1] is True on about half the bars, so 50 bars are plenty.
    "barssince": ("ta.barssince(c > ta.shift(c, 1))", 50, "line"),
    "valuewhen": ("ta.valuewhen(c > ta.shift(c, 1), c, 1)", 50, "line"),
    "rising": ("ta.rising(c, 3)", 4, "marker"),
    "falling": ("ta.falling(c, 3)", 4, "marker"),
    "rsi": ("ta.rsi(c, 14)", 20 * L + L, "line"),
    "tr": ("ta.tr(h, l, c)", 1, "line"),
    "atr": ("ta.atr(h, l, c, 14)", 20 * L + L, "line"),
    "stoch": ("ta.stoch(c, h, l, 14)", L, "line"),
    "cci": ("ta.cci(c, 14)", L, "line"),
    "macd": ("ta.macd(c)[0]", 10 * (26 + 9) + 26, "line"),
    "macd signal": ("ta.macd(c)[1]", 10 * (26 + 9) + 26 + 9, "line"),
    "mom": ("ta.mom(c, 10)", 10, "line"),
    "roc": ("ta.roc(c, 10)", 10, "line"),
    "stdev": ("ta.stdev(c, 20)", 20, "line"),
    "bb upper": ("ta.bb(c, 20)[1]", 20, "line"),
    "highest": ("ta.highest(h, 14)", L, "line"),
    "lowest": ("ta.lowest(l, 14)", L, "line"),
    "highestbars": ("ta.highestbars(h, 14)", L, "line"),
    "lowestbars": ("ta.lowestbars(l, 14)", L, "line"),
    "sum": ("ta.sum(c, 14)", L, "line"),
    "pivothigh": ("ta.pivothigh(h, 3, 3)", 7, "line"),
    "pivotlow": ("ta.pivotlow(l, 3, 3)", 7, "line"),
    "the package's sma": ("sma(c, 14)", L, "line"),
    "the package's ema": ("ema(c, 14)", 10 * L + L, "line"),
}
MARKER = 'Marker("x", "X", color="color")'


@pytest.mark.parametrize(("expr", "warm", "kind"), HELPERS.values(), ids=list(HELPERS))
def test_no_ta_helper_is_flagged_with_its_documented_warmup(expr, warm, kind):
    outputs = MARKER if kind == "marker" else LINE
    indicator = make(f"return {{'x': {expr}}}", warmup=warm, outputs=outputs)
    plan, ahead, behind = check(indicator, make_bars(1200))
    assert plan.cut and plan.start is not None, (plan.lookahead_skip, plan.warmup_skip)
    assert ahead == []
    assert behind == []


def test_cum_is_reported_as_never_agreeing_whatever_the_warmup():
    for warm in (0, 50, 300):
        _, ahead, behind = check(make("return {'x': ta.cum(c)}", warmup=warm), make_bars(1000))
        assert ahead == []
        (f,) = warnings(behind)
        assert "no warmup can fix" in f["message"], f["message"]


# Known limits: lookahead the check does NOT see. Pinned, so a change in what it sees is a
# decision, not an accident. The docs say the check finds some lookahead, not all.


def test_limit_bfill_on_the_warmup_is_not_flagged():
    # The first 19 bars take the value of bar 19: real lookahead, but only inside the
    # declared warmup, which neither check compares (both runs start on bar 0 for the
    # lookahead check; the warmup check skips the offset run's own warmup).
    _, ahead, behind = check(make("return {'x': ta.sma(c, 20).bfill()}", warmup=20), make_bars(400))
    assert ahead == []
    assert warnings(behind) == []


def test_limit_a_forward_read_that_does_not_change_the_result_is_not_flagged():
    # The highest close of the whole series is read on every bar (lookahead), but it lies
    # well before the last 50 bars, so removing them changes nothing.
    frame = make_bars(400)
    frame.loc[100, "c"] = 3000.0
    _, ahead, _ = check(make("return {'x': c / c.max()}"), frame)
    assert ahead == []


def test_limit_a_bar_count_branch_outside_the_window_is_not_flagged():
    # 400 and 350 bars are both above 100: the branch is never taken differently here.
    _, ahead, _ = check(make("return {'x': c if len(c) > 100 else c * 2}"), make_bars(400))
    assert ahead == []


# Warmup ---------------------------------------------------------------------------------


def test_a_short_warmup_is_reported_with_the_bars_it_needs():
    frame = make_bars(1000)
    _, ahead, behind = check(make("return {'x': ta.ema(c, 20)}", warmup=20), frame)
    assert ahead == []
    (f,) = warnings(behind)
    assert f["check"] == "warmup" and f["output"] == "x"
    assert abs(f["full"] - f["other"]) > 0
    assert "declared warmup of 20 bars" in f["message"]
    assert 20 < f["needed"] <= 10 * 20 + 20
    assert f"at least {f['needed']}" in f["message"]


def test_an_m7_file_without_a_warmup_is_told_how_many_bars_its_sma_needs():
    # GOOD (the CI-1 tests' file) declares no warmup: an SMA(5) needs 4 bars before it.
    _, ahead, behind = check(_compile("good.py", textwrap.dedent(GOOD)), make_bars(400))
    assert ahead == []
    (f,) = warnings(behind)
    assert f["needed"] == 4


def test_barssince_needs_its_condition_to_have_been_true_in_the_warmup():
    # docs/indicators.md said "none"; the check found otherwise, and the docs were fixed.
    body = "return {'x': ta.barssince(c > ta.shift(c, 1))}"
    _, _, behind = check(make(body, warmup=0), make_bars(1200))
    (f,) = warnings(behind)
    assert f["needed"] >= 1


def test_a_recursive_average_with_no_warmup_is_reported():
    _, _, behind = check(make("return {'x': ta.rma(c, 14)}", warmup=0), make_bars(1000))
    assert [f["output"] for f in warnings(behind)] == ["x"]


def test_the_offset_is_the_warmup_or_50_and_leaves_bars_to_compare():
    assert _checks.plan(1000, 0).start == 50
    assert _checks.plan(1000, 120).start == 120
    p = _checks.plan(600, 294)  # 294 + 294 would leave 12 bars: the offset shrinks
    assert p.start == 600 - 294 - 150
    assert _checks.plan(100, 70).start == 5  # 25 bars compared, from bar 75
    assert _checks.plan(100, 80).start is None
    assert "too few bars" in _checks.plan(100, 80).warmup_skip


def test_short_inputs_remove_fewer_bars_and_very_short_ones_are_skipped():
    assert _checks.plan(400, 0).cut == 50
    assert _checks.plan(100, 0).cut == 25
    assert _checks.plan(8, 0).cut == 2
    assert _checks.plan(7, 0).cut is None
    assert "too few bars" in _checks.plan(7, 0).lookahead_skip


# Equality: gaps, infinities, tolerance, exact booleans ------------------------------------

TWO = LINE + ', Marker("m", "M", color="color")'


def compare(full_x, part_x, full_m=None, part_m=None):
    indicator = make("return {}", outputs=TWO)
    n, k = len(full_x), len(part_x)
    full = {"x": np.array(full_x, float), "m": np.array(full_m or [0.0] * n, float)}
    part = {"x": np.array(part_x, float), "m": np.array(part_m or [0.0] * k, float)}
    return _checks.lookahead_findings(indicator, full, part, np.arange(n) * 60.0)


def test_a_gap_equals_a_gap_and_an_infinity_is_a_gap():
    assert compare([math.nan, 1.0, 2.0], [math.nan, 1.0]) == []
    assert compare([math.inf, 1.0, 2.0], [math.nan, 1.0]) == []
    assert compare([-math.inf, 1.0, 2.0], [math.inf, 1.0]) == []


def test_a_gap_against_a_number_differs():
    (f,) = compare([math.nan, 1.0, 2.0], [0.5, 1.0])
    assert (f["t"], f["full"], f["other"]) == (0.0, None, 0.5)


def test_the_tolerance_is_relative_to_the_largest_value_of_the_output():
    scale = 1000.0  # tolerance 1e-9 x 1000 = 1e-6, from the full run (its last bar)
    assert compare([1.0, 0.0, scale], [1.0 + 0.9e-6, 0.0]) == []
    assert len(compare([1.0, 0.0, scale], [1.0 + 1.1e-6, 0.0])) == 1
    # A scale of zero (an output that is all zeros and gaps) still compares exactly.
    assert len(compare([0.0, 0.0, 0.0], [1e-300, 0.0])) == 1


def test_markers_are_compared_exactly():
    assert len(compare([1.0, 1.0, 1.0], [1.0, 1.0], [0.0, 1.0, 0.0], [1.0, 1.0])) == 1


def test_the_warmup_tolerance_is_looser():
    indicator = make("return {}", warmup=2)
    full = {"x": np.array([5.0] * 30)}
    plan = _checks.Plan(n=30, warmup=2, cut=7, start=5)
    near = {"x": np.array([5.0] * 25) + 4e-7}  # 1e-7 x 5 = 5e-7
    far = {"x": np.array([5.0] * 25) + 6e-7}
    t = np.arange(30) * 60.0
    assert _checks.warmup_findings(indicator, full, near, plan, t) == []
    assert len(_checks.warmup_findings(indicator, full, far, plan, t)) == 1


def test_the_warmup_scale_is_the_whole_full_run_not_only_the_compared_bars():
    indicator = make("return {}", warmup=2)
    plan = _checks.Plan(n=30, warmup=2, cut=7, start=5)
    full = {"x": np.array([100.0] + [1.0] * 29)}  # the largest value is before bar 7
    offset = {"x": np.array([1.0] * 25) + 5e-6}  # within 1e-7 x 100, not 1e-7 x 1
    assert _checks.warmup_findings(indicator, full, offset, plan, np.arange(30) * 60.0) == []


def test_the_offset_runs_own_warmup_is_not_compared():
    indicator = make("return {}", warmup=3)
    plan = _checks.Plan(n=30, warmup=3, cut=7, start=5)
    full = {"x": np.arange(30.0)}
    offset = {"x": np.arange(5.0, 30.0)}
    offset["x"][:3] = -1.0  # its first 3 bars are its warmup
    t = np.arange(30) * 60.0
    assert _checks.warmup_findings(indicator, full, offset, plan, t) == []
    offset["x"][3] = -1.0  # the first compared bar
    (f,) = _checks.warmup_findings(indicator, full, offset, plan, t)
    assert f["t"] == t[8]


def test_the_lower_bound_counts_from_the_last_differing_bar():
    indicator = make("return {}", warmup=1)
    plan = _checks.Plan(n=40, warmup=1, cut=10, start=10)
    full = {"x": np.arange(40.0)}
    offset = {"x": np.arange(10.0, 40.0)}
    offset["x"][[2, 6]] += 1.0  # full bars 12 and 16 differ; bar 16 had 6 bars before it
    (f,) = _checks.warmup_findings(indicator, full, offset, plan, np.arange(40) * 60.0)
    assert f["needed"] == 7
    assert f["t"] == 12 * 60.0


# A marker that flips on a near-tie in the warmup check (the owner's point 3) ---------------

TIE = """
s = c.cumsum()
s = s - ta.shift(s, 5)  # a 5-bar sum whose rounding depends on where the run starts
return {'x': s, 'm': s > params['k']}
"""


def tie_case(outputs):
    """A threshold that sits between the two runs' values of one bar, found by running."""
    frame = make_bars(400)
    params = "\n    , Float('k', 0.0)"
    probe = make(TIE, warmup=5, outputs=TWO, params=params)
    p = probe.normalize({})
    plan = _checks.plan(len(frame), 5)
    full = run(probe, frame, p)["x"]
    offset = run(probe, frame.iloc[plan.start :], p)["x"]
    shared = full[plan.start :]
    diff = [i for i in range(5, len(offset)) if shared[i] != offset[i]]
    assert diff, "no bar rounds differently in this data"
    i = diff[len(diff) // 2]
    k = min(shared[i], offset[i])  # one run is above k, the other equal to it
    indicator = make(
        TIE if outputs == TWO else TIE.replace("'x': s, ", ""),
        warmup=5,
        outputs=outputs,
        params=params,
    )
    return check(indicator, frame, {"k": k})


def test_a_marker_flipping_on_a_near_tie_while_its_lines_agree_is_a_note():
    _, ahead, behind = tie_case(TWO)
    assert ahead == []  # the lookahead runs start on the same bar: bit-identical
    assert warnings(behind) == []
    (note,) = behind
    assert note["output"] == "m" and note["level"] == "note"
    assert "near-tie" in note["message"]


def test_a_marker_only_script_flipping_on_a_near_tie_is_still_a_warning():
    # Nothing to tell a near-tie from a short warmup by: the documented limit of the rule.
    _, _, behind = tie_case('Marker("m", "M", color="color")')
    (f,) = warnings(behind)
    assert f["output"] == "m"
    assert "near-tie" in f["message"]


def test_a_marker_differing_where_its_lines_differ_stays_a_warning():
    # An EMA with too short a warmup, and a marker on a threshold between the two runs'
    # values of the first compared bar: both differ, so the marker is not a near-tie.
    frame = make_bars(1000)
    body = "e = ta.ema(c, 20)\nreturn {'x': e, 'm': e > params['k']}"
    probe = make(body, warmup=20, outputs=TWO, params="\n    , Float('k', 0.0)")
    p = probe.normalize({})
    plan = _checks.plan(1000, 20)
    a = run(probe, frame, p)["x"][plan.start + 20]
    b = run(probe, frame.iloc[plan.start :], p)["x"][20]
    assert abs(a - b) > 1e-3
    _, _, behind = check(probe, frame, {"k": (a + b) / 2})
    assert {f["output"] for f in warnings(behind)} == {"x", "m"}


# Time budget ----------------------------------------------------------------------------


def test_a_run_must_fit_in_most_of_the_deadline():
    assert _checks.fits(2.0, 5.0)
    assert _checks.fits(4.0, 5.0)
    assert not _checks.fits(4.01, 5.0)


def test_the_state_says_whether_anything_was_found():
    assert _checks.state([]) == "ok"
    assert _checks.state([{"level": "note"}]) == "ok"
    assert _checks.state([{"level": "note"}, {"level": "warning"}]) == "warned"
