"""The `ta` helper library (CI-2 step a), group 1: averages and series helpers.

Every expected value comes from a plain Python loop written from the public formula, or from
a number worked out by hand. None of it calls the helper code. Each helper also gets a
no-lookahead test (the value at bar k-1 is the same whether the series stops there or goes
on), a test with gaps and short inputs, and a check that the input's index is kept.
"""

import math
import random

import numpy as np
import pandas as pd
import pytest

from openchart.indicators import ema, sma, ta

NAN = float("nan")

# Reference implementations: loops over lists, no pandas


def isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def ref_sma(xs, length):
    out = [NAN] * len(xs)
    for i in range(length - 1, len(xs)):
        w = xs[i - length + 1 : i + 1]
        if not any(isnan(v) for v in w):
            out[i] = sum(w) / length
    return out


def ref_recursive(xs, length, alpha):
    """TradingView's documented ema/rma: `sum := na(sum[1]) ? sma(src, length) :
    alpha * src + (1 - alpha) * sum[1]`. A gap in the input gives na at the gap, and the
    average starts again once `length` clean values are back."""
    out = [NAN] * len(xs)
    for i, x in enumerate(xs):
        if isnan(x):
            continue
        prev = out[i - 1] if i > 0 else NAN
        if isnan(prev):
            out[i] = ref_sma(xs[: i + 1], length)[i]
        else:
            out[i] = alpha * x + (1 - alpha) * prev
    return out


def ref_ema(xs, length):
    return ref_recursive(xs, length, 2 / (length + 1))


def ref_rma(xs, length):
    return ref_recursive(xs, length, 1 / length)


def ref_wma(xs, length):
    out = [NAN] * len(xs)
    norm = length * (length + 1) / 2
    for i in range(length - 1, len(xs)):
        w = xs[i - length + 1 : i + 1]
        if not any(isnan(v) for v in w):
            out[i] = sum((j + 1) * v for j, v in enumerate(w)) / norm
    return out


def ref_vwma(xs, vs, length):
    out = [NAN] * len(xs)
    for i in range(length - 1, len(xs)):
        w = xs[i - length + 1 : i + 1]
        u = vs[i - length + 1 : i + 1]
        if any(isnan(v) for v in w + u) or sum(u) == 0:
            continue
        out[i] = sum(a * b for a, b in zip(w, u, strict=True)) / sum(u)
    return out


def ref_hma(xs, length):
    half = max(1, length // 2)
    root = max(1, math.floor(math.sqrt(length) + 0.5))
    fast, slow = ref_wma(xs, half), ref_wma(xs, length)
    raw = [NAN if isnan(a) or isnan(b) else 2 * a - b for a, b in zip(fast, slow, strict=True)]
    return ref_wma(raw, root)


def same(got, want):
    got = list(got)
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        if isnan(w):
            assert isnan(g), f"bar {i}: got {g}, want NaN"
        else:
            assert not isnan(g), f"bar {i}: got NaN, want {w}"
            assert g == pytest.approx(w, rel=1e-12, abs=1e-12), f"bar {i}"


def prices(n=300, seed=1):
    rng = random.Random(seed)
    return [2000 + rng.gauss(0, 5) for _ in range(n)]


def with_gaps(xs, at=(40, 41, 120)):
    xs = list(xs)
    for i in at:
        xs[i] = NAN
    return xs


def volumes(n=300, seed=2):
    rng = random.Random(seed)
    return [float(rng.randint(1, 500)) for _ in range(n)]


# Averages against the references

LENGTHS = [1, 2, 3, 5, 14, 50]


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.parametrize(
    ("name", "helper", "ref"),
    [
        ("sma", ta.sma, ref_sma),
        ("ema", ta.ema, ref_ema),
        ("rma", ta.rma, ref_rma),
        ("wma", ta.wma, ref_wma),
        ("hma", ta.hma, ref_hma),
    ],
)
@pytest.mark.parametrize("gaps", [False, True])
def test_averages_match_the_references(name, helper, ref, length, gaps):
    xs = prices()
    if gaps:
        xs = with_gaps(xs)
    same(helper(pd.Series(xs), length), ref(xs, length))


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.parametrize("gaps", [False, True])
def test_vwma_matches_the_reference(length, gaps):
    xs, vs = prices(), volumes()
    if gaps:
        xs = with_gaps(xs)
    same(ta.vwma(pd.Series(xs), pd.Series(vs), length), ref_vwma(xs, vs, length))


def test_vwma_with_no_volume_in_the_window_is_a_gap_not_infinity():
    got = ta.vwma(pd.Series([1.0, 2, 3, 4]), pd.Series([0.0, 0, 5, 5]), 2)
    same(got, [NAN, NAN, 3.0, 3.5])


def test_the_top_level_sma_and_ema_are_the_ta_ones():
    assert ta.sma is sma
    assert ta.ema is ema


# Averages by hand


def test_wma_by_hand():
    # length 3, weights 1, 2, 3 (newest heaviest), divided by 6.
    same(ta.wma(pd.Series([1.0, 2, 3, 4]), 3), [NAN, NAN, 14 / 6, 20 / 6])


def test_rma_by_hand():
    # length 3: alpha 1/3, seed (1 + 2 + 3) / 3 = 2, then 2 + (4 - 2) / 3, then onward.
    want = [NAN, NAN, 2.0, 2 + 2 / 3, (2 + 2 / 3) + (5 - (2 + 2 / 3)) / 3]
    same(ta.rma(pd.Series([1.0, 2, 3, 4, 5]), 3), want)


def test_ema_and_rma_start_with_the_sma_of_the_first_length_values():
    xs = pd.Series(prices(40))
    for helper in (ta.ema, ta.rma):
        got = helper(xs, 10)
        assert got.iloc[:9].isna().all()
        assert got.iloc[9] == pytest.approx(xs.iloc[:10].mean())


def test_hma_of_a_straight_line_is_the_line_itself():
    # Worked out by hand for length 4 (halves of 2, root of 2): the weighted averages lag
    # by 1/3 and 1, and 2*wma(2) - wma(4) then wma(2) of that cancels the lag exactly.
    ramp = pd.Series([float(t) for t in range(30)])
    got = ta.hma(ramp, 4)
    assert got.iloc[:4].isna().all()  # first value at bar (4 - 1) + (2 - 1)
    assert got.iloc[4:].tolist() == pytest.approx(ramp.iloc[4:].tolist())


def test_hma_rounding_of_the_lengths():
    # Odd length 9: half is 4 (rounded down), root is 3. Length 1 stays usable.
    xs = prices(60)
    same(ta.hma(pd.Series(xs), 9), ref_hma(xs, 9))
    same(ta.hma(pd.Series(xs), 1), xs)


def test_a_gap_in_the_input_restarts_ema_and_rma():
    xs = [float(v) for v in range(1, 13)]
    xs[5] = NAN
    got = ta.ema(pd.Series(xs), 3)
    assert got.iloc[:2].isna().all()
    assert not math.isnan(got.iloc[2]) and not math.isnan(got.iloc[4])
    assert math.isnan(got.iloc[5])
    assert got.iloc[6:8].isna().all()  # the average starts again with 3 clean values
    assert got.iloc[8] == pytest.approx(sum(xs[6:9]) / 3)


# Series helpers by hand


def test_shift_and_change():
    x = pd.Series([1.0, 2, 4, 8])
    same(ta.shift(x, 1), [NAN, 1.0, 2.0, 4.0])
    same(ta.shift(x, 2), [NAN, NAN, 1.0, 2.0])
    same(ta.change(x), [NAN, 1.0, 2.0, 4.0])
    same(ta.change(x, 2), [NAN, NAN, 3.0, 6.0])


@pytest.mark.parametrize("n", [0, -1, -5])
def test_shift_refuses_the_future(n):
    with pytest.raises(ValueError, match="future"):
        ta.shift(pd.Series([1.0, 2.0]), n)
    with pytest.raises(ValueError, match="future"):
        ta.change(pd.Series([1.0, 2.0]), n)


def test_nz():
    x = pd.Series([NAN, 1.0, NAN])
    same(ta.nz(x), [0.0, 1.0, 0.0])
    same(ta.nz(x, 7), [7.0, 1.0, 7.0])


def test_crossover_crossunder_cross():
    a = pd.Series([1.0, 2, 3, 2, 1])
    b = pd.Series([2.0, 2, 2, 2, 2])
    assert ta.crossover(a, b).tolist() == [False, False, True, False, False]
    assert ta.crossunder(a, b).tolist() == [False, False, False, False, True]
    assert ta.cross(a, b).tolist() == [False, False, True, False, True]
    assert ta.crossover(a, b).dtype == bool


def test_cross_against_a_number():
    rsi_like = pd.Series([25.0, 29, 31, 35, 29])
    assert ta.crossover(rsi_like, 30).tolist() == [False, False, True, False, False]
    assert ta.crossunder(rsi_like, 30).tolist() == [False, False, False, False, True]
    assert ta.crossunder(30, rsi_like).tolist() == [False, False, True, False, False]


def test_touching_and_then_leaving_counts_as_a_cross_once():
    # a == b on bar 1, above on bar 2: one crossover, on bar 2 (previous bar a <= b).
    a = pd.Series([1.0, 2, 3])
    b = pd.Series([2.0, 2, 2])
    assert ta.crossover(a, b).tolist() == [False, False, True]


def test_a_gap_is_never_a_cross():
    a = pd.Series([1.0, NAN, 3.0, 4.0])
    b = pd.Series([2.0, 2, 2, 2])
    assert not ta.crossover(a, b).any()  # bar 2 follows a gap, so there is no "previous"
    assert ta.crossover(a, b).dtype == bool


def test_barssince():
    cond = pd.Series([False, True, False, False, True, False])
    same(ta.barssince(cond), [NAN, 0.0, 1.0, 2.0, 0.0, 1.0])


def test_valuewhen():
    cond = pd.Series([False, True, False, True, False])
    x = pd.Series([10.0, 11, 12, 13, 14])
    same(ta.valuewhen(cond, x), [NAN, 11.0, 11.0, 13.0, 13.0])
    same(ta.valuewhen(cond, x, n=1), [NAN, NAN, NAN, 11.0, 11.0])
    same(ta.valuewhen(cond, x, n=2), [NAN] * 5)


def test_rising_and_falling_need_every_step_to_go_the_same_way():
    # n steps, n + 1 values: x[0] > x[1] > x[2] for n = 2 (checked in TradingView).
    x = pd.Series([1.0, 2, 3, 2, 4])
    assert ta.rising(x, 2).tolist() == [False, False, True, False, False]
    y = pd.Series([5.0, 4, 3, 4, 2])
    assert ta.falling(y, 2).tolist() == [False, False, True, False, False]
    assert ta.rising(pd.Series([1.0, 2, 3, 4, 5]), 4).tolist() == [False] * 4 + [True]
    assert ta.rising(pd.Series([1.0, 2, 2, 3]), 2).tolist() == [False] * 4  # a flat step breaks it


def test_the_series_that_separates_each_step_from_higher_than_all():
    # [2, 1, 3] at n = 2: 3 is higher than both earlier values, but 1 -> 3 follows 2 -> 1, a
    # step down, so it is not rising. Mirrored for falling. "Higher than all" would say True.
    assert ta.rising(pd.Series([2.0, 1, 3]), 2).tolist() == [False, False, False]
    assert ta.falling(pd.Series([2.0, 3, 1]), 2).tolist() == [False, False, False]
    assert ta.rising(pd.Series([1.0, 3, 2, 4]), 2).tolist() == [False] * 4


def test_rising_with_a_gap_in_the_window_is_false():
    assert ta.rising(pd.Series([1.0, NAN, 3.0, 4.0]), 2).tolist() == [False] * 4


# Every helper: no lookahead, gaps, short inputs, index, bad lengths

X = pd.Series(with_gaps(prices(120), at=(30, 31, 90)))
V = pd.Series(volumes(120))

CASES = {
    "sma": lambda x: ta.sma(x, 5),
    "ema": lambda x: ta.ema(x, 5),
    "rma": lambda x: ta.rma(x, 5),
    "wma": lambda x: ta.wma(x, 5),
    "vwma": lambda x: ta.vwma(x, V.iloc[: len(x)], 5),
    "hma": lambda x: ta.hma(x, 7),
    "shift": lambda x: ta.shift(x, 3),
    "change": lambda x: ta.change(x, 2),
    "nz": lambda x: ta.nz(x),
    "crossover": lambda x: ta.crossover(x, ta.sma(x, 5)),
    "crossunder": lambda x: ta.crossunder(x, ta.sma(x, 5)),
    "cross": lambda x: ta.cross(x, 2000),
    "barssince": lambda x: ta.barssince(x > 2000),
    "valuewhen": lambda x: ta.valuewhen(x > 2000, x, 1),
    "rising": lambda x: ta.rising(x, 3),
    "falling": lambda x: ta.falling(x, 3),
}


def value_at(series, i):
    v = series.iloc[i]
    return None if isnan(v) else v


@pytest.mark.parametrize("name", CASES)
def test_no_lookahead_the_value_at_a_bar_does_not_depend_on_later_bars(name):
    fn = CASES[name]
    full = fn(X)
    for k in range(1, len(X) + 1):
        cut = fn(X.iloc[:k])
        assert len(cut) == k
        assert value_at(cut, k - 1) == pytest.approx(value_at(full, k - 1)), f"k={k}"


@pytest.mark.parametrize("name", CASES)
def test_changing_later_bars_leaves_earlier_values_alone(name):
    fn = CASES[name]
    other = X.copy()
    other.iloc[70:] = other.iloc[70:] * 1.5 + 3
    a, b = fn(X), fn(other)
    for i in range(70):
        assert value_at(a, i) == pytest.approx(value_at(b, i)), f"bar {i}"


@pytest.mark.parametrize("name", CASES)
def test_short_and_empty_inputs_give_gaps_not_errors(name):
    fn = CASES[name]
    for n in (0, 1, 2, 3):
        out = fn(X.iloc[:n].reset_index(drop=True))
        assert len(out) == n
    one = fn(pd.Series([2001.0]))
    assert len(one) == 1


@pytest.mark.parametrize("name", CASES)
def test_the_index_of_the_input_is_kept(name):
    fn = CASES[name]
    shifted = X.copy()
    shifted.index = pd.RangeIndex(1000, 1000 + len(X))
    assert fn(shifted).index.equals(shifted.index)


@pytest.mark.parametrize("name", ["sma", "ema", "rma", "wma", "hma", "vwma"])
def test_averages_give_all_gaps_when_there_are_fewer_bars_than_the_length(name):
    short = pd.Series([1.0, 2.0, 3.0])
    out = ta.vwma(short, short, 10) if name == "vwma" else getattr(ta, name)(short, 10)
    assert out.isna().all() and len(out) == 3


@pytest.mark.parametrize("name", ["sma", "ema", "rma", "wma", "hma"])
@pytest.mark.parametrize("bad", [0, -3, 2.5, "5", True])
def test_a_bad_length_is_refused_with_a_message(name, bad):
    with pytest.raises(ValueError, match="length"):
        getattr(ta, name)(pd.Series([1.0, 2.0]), bad)


def test_all_gap_input_stays_all_gaps():
    nothing = pd.Series([NAN] * 10)
    for helper in (ta.sma, ta.ema, ta.rma, ta.wma, ta.hma):
        assert helper(nothing, 3).isna().all()
    assert not ta.crossover(nothing, 1.0).any()
    assert ta.barssince(nothing > 0).isna().all()


def test_integer_and_list_inputs_are_accepted():
    assert ta.sma(pd.Series([1, 2, 3, 4]), 2).tolist()[1:] == [1.5, 2.5, 3.5]
    assert np.isnan(ta.ema([1, 2, 3], 5)).all()
