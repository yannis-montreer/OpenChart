"""The `ta` helper library (CI-2 step a), group 3: windows and pivots.

References are loops over lists, written from the definitions. Conventions decided here
(docs/decisions.md): a gap anywhere in a window makes the window's value a gap; `highestbars`
and `lowestbars` return Pine's offsets (0 for the current bar, -1 for the one before, ...)
and, on a tie, the most recent bar; a pivot is reported on the bar where it is confirmed,
`right` bars after the pivot, with the pivot's price, and is a gap everywhere else.
"""

import random

import pandas as pd
import pytest

from openchart.indicators import _ta, ta
from tests.test_ta import NAN, X, isnan, same, value_at


def S(xs):
    return pd.Series(xs, dtype="float64")


def window(xs, i, length):
    return xs[i - length + 1 : i + 1] if i - length + 1 >= 0 else None


def ref_extreme(xs, length, pick):
    out = [NAN] * len(xs)
    for i in range(len(xs)):
        w = window(xs, i, length)
        if w is not None and not any(isnan(v) for v in w):
            out[i] = pick(w)
    return out


def ref_highest(xs, length):
    return ref_extreme(xs, length, max)


def ref_lowest(xs, length):
    return ref_extreme(xs, length, min)


def ref_bars(xs, length, better):
    """Offset of the best value in the window: 0 for this bar, -1 for the one before, and so
    on. Looking from the newest bar back and replacing only on a strictly better value
    keeps the most recent bar on a tie."""
    out = [NAN] * len(xs)
    for i in range(len(xs)):
        w = window(xs, i, length)
        if w is None or any(isnan(v) for v in w):
            continue
        best_back = 0
        for back in range(1, length):
            if better(w[length - 1 - back], w[length - 1 - best_back]):
                best_back = back
        out[i] = -best_back
    return out


def ref_sum(xs, length):
    out = [NAN] * len(xs)
    for i in range(len(xs)):
        w = window(xs, i, length)
        if w is not None and not any(isnan(v) for v in w):
            total = 0.0
            for v in w:
                total += v
            out[i] = total
    return out


def ref_cum(xs):
    out, total = [], 0.0
    for v in xs:
        if isnan(v):
            out.append(NAN)
        else:
            total += v
            out.append(total)
    return out


def ref_pivot(xs, left, right, high):
    """Without ties (the random data has none), whatever the tie rule: the bar `right` bars
    back is a pivot when it is above (below) every one of the `left` bars before it and the
    `right` bars after it. Its price is reported on the latest of those bars."""
    out = [NAN] * len(xs)
    for c in range(len(xs)):
        p = c - right
        if p - left < 0:
            continue
        around = xs[p - left : c + 1]
        if any(isnan(v) for v in around):
            continue
        others = around[:left] + around[left + 1 :]
        if all(xs[p] > v for v in others) if high else all(xs[p] < v for v in others):
            out[c] = xs[p]
    return out


def noisy(n=300, seed=5, gaps=False):
    rng = random.Random(seed)
    xs = [2000 + rng.gauss(0, 5) for _ in range(n)]  # no two values equal
    if gaps:
        for i in (40, 41, 120):
            xs[i] = NAN
    return xs


LENGTHS = [1, 2, 3, 7, 20]


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.parametrize("gaps", [False, True])
def test_windows_match_the_references(length, gaps):
    xs = noisy(gaps=gaps)
    s = S(xs)
    same(ta.highest(s, length), ref_highest(xs, length))
    same(ta.lowest(s, length), ref_lowest(xs, length))
    same(ta.highestbars(s, length), ref_bars(xs, length, lambda a, b: a > b))
    same(ta.lowestbars(s, length), ref_bars(xs, length, lambda a, b: a < b))
    same(ta.sum(s, length), ref_sum(xs, length))


def test_cum_matches_the_reference():
    for gaps in (False, True):
        xs = noisy(gaps=gaps)
        same(ta.cum(S(xs)), ref_cum(xs))


def test_windows_by_hand():
    same(ta.highest(S([1, 3, 2, 3, 0]), 2), [NAN, 3.0, 3.0, 3.0, 3.0])  # the current bar counts
    same(ta.lowest(S([1, 3, 2, 3, 0]), 3), [NAN, NAN, 1.0, 2.0, 0.0])
    same(ta.sum(S([1, 2, 3, 4]), 2), [NAN, 3.0, 5.0, 7.0])
    same(ta.cum(S([1, 2, 3, 4])), [1.0, 3.0, 6.0, 10.0])


def test_a_gap_in_a_window_makes_its_value_a_gap_and_only_while_it_is_inside():
    x = S([1, NAN, 3, 4, 5])
    for helper in (ta.highest, ta.lowest, ta.sum, ta.highestbars, ta.lowestbars):
        got = helper(x, 2).tolist()
        assert all(isnan(v) for v in got[:3]), helper
        assert not isnan(got[3]) and not isnan(got[4]), helper


def test_cum_with_a_gap_is_a_gap_there_and_carries_on():
    same(ta.cum(S([1, 2, NAN, 4])), [1.0, 3.0, NAN, 7.0])
    same(ta.cum(S([NAN, 1, 2])), [NAN, 1.0, 3.0])


def test_cum_depends_on_where_the_history_starts():
    # Unlike every other helper, no warmup can make it agree: the total includes all history.
    x = S(noisy(60))
    assert ta.cum(x).iloc[59] != pytest.approx(ta.cum(x.iloc[10:]).iloc[49])


def test_highestbars_and_lowestbars_by_hand_and_on_ties():
    # Window of 3 at bar 3 holds 3, 2, 3: the highest is at offsets -2 and 0 -> the recent one.
    same(ta.highestbars(S([1, 3, 2, 3]), 3), [NAN, NAN, -1.0, 0.0])
    same(ta.lowestbars(S([2, 1, 3, 1]), 3), [NAN, NAN, -1.0, 0.0])
    same(ta.highestbars(S([5, 5, 5, 5]), 3), [NAN, NAN, 0.0, 0.0])  # all equal: this bar
    same(ta.lowestbars(S([5, 4, 3, 2]), 4), [NAN, NAN, NAN, 0.0])
    same(ta.highestbars(S([9, 1, 1, 1]), 4), [NAN, NAN, NAN, -3.0])  # the oldest bar of the window


# Pivots


def test_pivots_match_the_references():
    for gaps in (False, True):
        xs = noisy(gaps=gaps)
        for left, right in [(1, 1), (2, 3), (5, 2), (10, 10)]:
            same(ta.pivothigh(S(xs), left, right), ref_pivot(xs, left, right, True))
            same(ta.pivotlow(S(xs), left, right), ref_pivot(xs, left, right, False))
    assert any(not isnan(v) for v in ta.pivothigh(S(noisy()), 3, 3))  # the test is not empty


def test_pivotlow_is_pivothigh_of_the_mirror_image():
    xs = S(noisy())
    same(ta.pivotlow(xs, 3, 4), (-ta.pivothigh(-xs, 3, 4)).tolist())


def test_a_pivot_is_reported_right_bars_late_and_never_earlier():
    # The peak 9 is bar 3. With left 3 and right 2 it can only be known at bar 5.
    x = [1.0, 2, 3, 9, 4, 3, 2, 1, 0.5, 0.2]
    full = ta.pivothigh(S(x), 3, 2)
    assert full.iloc[5] == 9.0
    assert full.drop(index=5).isna().all()  # a gap everywhere else, in particular at bar 3
    for k in range(1, len(x) + 1):  # what a chart sees when the series stops at k bars
        cut = ta.pivothigh(S(x[:k]), 3, 2)
        if k <= 5:
            assert cut.isna().all(), f"k={k}: a pivot shown before it can be confirmed"
        else:
            assert cut.iloc[5] == 9.0 and cut.drop(index=5).isna().all(), f"k={k}"
    # What comes after the confirming bar cannot take it back; what comes before can.
    later = list(x)
    later[6:] = [100.0] * 4
    assert ta.pivothigh(S(later), 3, 2).iloc[5] == 9.0
    earlier = list(x)
    earlier[4] = 10.0  # one of the 2 bars after the 9 is higher: the 9 is no pivot, the 10 is
    moved = ta.pivothigh(S(earlier), 3, 2)
    assert moved.iloc[5] != 9.0 and moved.iloc[6] == 10.0
    assert moved.drop(index=6).isna().all()


# UNCONFIRMED: how ties count in a pivot. Check these cases in TradingView; to flip the rule,
# change `_PIVOT_STRICT` in _ta.py (one line) and keep the row of EXPECTED_BY_RULE that
# TradingView agrees with as the one the first assert uses (it reads the library's setting).
# The rule is (left side strict, right side strict): strict means a bar equal to the pivot on
# that side stops it being a pivot.
PLATEAU = [1.0, 5, 5, 1]  # two equal highs side by side, left = right = 1
INSIDE = [5.0, 1, 5, 1, 5]  # equal highs on both sides of the middle one, left = right = 2
EXPECTED_BY_RULE = {
    # rule: (pivothigh(PLATEAU, 1, 1), pivothigh(INSIDE, 2, 2))
    (True, True): ([NAN, NAN, NAN, NAN], [NAN] * 5),  # no tie ever makes a pivot
    (False, False): ([NAN, NAN, 5.0, 5.0], [NAN, NAN, NAN, NAN, 5.0]),  # both plateau bars
    (True, False): ([NAN, NAN, 5.0, NAN], [NAN] * 5),  # the first of equal highs
    (False, True): ([NAN, NAN, NAN, 5.0], [NAN] * 5),  # the last of equal highs
}


def test_pivot_tie_rule_unconfirmed_against_tradingview():
    plateau, inside = EXPECTED_BY_RULE[_ta._PIVOT_STRICT]
    same(ta.pivothigh(S(PLATEAU), 1, 1), plateau)
    same(ta.pivothigh(S(INSIDE), 2, 2), inside)
    assert _ta._PIVOT_STRICT == (True, True)  # the rule in force today; delete with the flip


@pytest.mark.parametrize("rule", list(EXPECTED_BY_RULE))
def test_every_tie_rule_works_by_changing_the_one_setting(rule, monkeypatch):
    monkeypatch.setattr(_ta, "_PIVOT_STRICT", rule)
    plateau, inside = EXPECTED_BY_RULE[rule]
    same(ta.pivothigh(S(PLATEAU), 1, 1), plateau)
    same(ta.pivothigh(S(INSIDE), 2, 2), inside)
    # The low is the mirror image under the same rule.
    same(ta.pivotlow(S([-v for v in PLATEAU]), 1, 1), [-v for v in plateau])


# Every helper: no lookahead at every cut, gaps, short inputs, index, bad arguments

GROUP3 = {
    "highest": lambda x: ta.highest(x, 6),
    "lowest": lambda x: ta.lowest(x, 6),
    "highestbars": lambda x: ta.highestbars(x, 6),
    "lowestbars": lambda x: ta.lowestbars(x, 6),
    "sum": lambda x: ta.sum(x, 6),
    "cum": lambda x: ta.cum(x),
    "pivothigh": lambda x: ta.pivothigh(x, 3, 2),
    "pivotlow": lambda x: ta.pivotlow(x, 2, 3),
}


@pytest.mark.parametrize("name", GROUP3)
def test_no_lookahead_gaps_short_inputs_and_the_index(name):
    fn = GROUP3[name]
    full = fn(X)
    for k in range(1, len(X) + 1):  # the value at bar k - 1 with and without what follows
        assert value_at(fn(X.iloc[:k]), k - 1) == pytest.approx(value_at(full, k - 1)), f"k={k}"
    changed = X.copy()
    changed.iloc[70:] = changed.iloc[70:] * 1.5 + 3
    other = fn(changed)
    for i in range(70):
        assert value_at(full, i) == pytest.approx(value_at(other, i)), f"bar {i}"
    for n in (0, 1, 2, 3):
        assert len(fn(X.iloc[:n].reset_index(drop=True))) == n
    moved = X.copy()
    moved.index = pd.RangeIndex(1000, 1000 + len(X))
    assert fn(moved).index.equals(moved.index)


@pytest.mark.parametrize("bad", [0, -3, 2.5, "5", True])
def test_a_bad_length_is_refused(bad):
    one = S([1.0, 2.0])
    for call in (
        lambda: ta.highest(one, bad),
        lambda: ta.lowest(one, bad),
        lambda: ta.highestbars(one, bad),
        lambda: ta.lowestbars(one, bad),
        lambda: ta.sum(one, bad),
    ):
        with pytest.raises(ValueError, match="length"):
            call()
    for helper in (ta.pivothigh, ta.pivotlow):
        with pytest.raises(ValueError, match="left"):
            helper(one, bad, 1)
        with pytest.raises(ValueError, match="right"):
            helper(one, 1, bad)


def test_fewer_bars_than_the_window_is_all_gaps():
    short = S([1.0, 2.0, 3.0])
    for out in (
        ta.highest(short, 10),
        ta.lowest(short, 10),
        ta.highestbars(short, 10),
        ta.lowestbars(short, 10),
        ta.sum(short, 10),
        ta.pivothigh(short, 5, 5),
        ta.pivotlow(short, 5, 5),
    ):
        assert out.isna().all() and len(out) == 3
