"""The `ta` helper library (CI-2 step a), group 2: oscillators and volatility.

References are loops over lists, written from the public formulas (Wilder's book for RSI,
true range and ATR; the Pine reference's definitions for the rest), plus numbers worked out
by hand and, for RSI, Wilder's published worked example. The shared reference loops for the
averages are in test_ta.py.
"""

import math
import random

import pandas as pd
import pytest

from openchart.indicators import ta
from tests.test_ta import (
    NAN,
    X,
    isnan,
    ref_ema,
    ref_rma,
    ref_sma,
    same,
    value_at,
)


def S(xs):
    return pd.Series(xs, dtype="float64")


def ref_tr(h, low, c):
    """Wilder's true range. The first bar, and any bar whose previous close is missing, has
    high - low (what TradingView's ta.atr uses)."""
    out = [NAN] * len(h)
    for i in range(len(h)):
        if isnan(h[i]) or isnan(low[i]):
            continue
        if i == 0 or isnan(c[i - 1]):
            out[i] = h[i] - low[i]
        else:
            out[i] = max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
    return out


def ref_atr(h, low, c, length):
    return ref_rma(ref_tr(h, low, c), length)


def ref_rsi(xs, length):
    gains, losses = [NAN], [NAN]
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        gains.append(NAN if isnan(d) else max(d, 0.0))
        losses.append(NAN if isnan(d) else max(-d, 0.0))
    up, down = ref_rma(gains, length), ref_rma(losses, length)
    out = []
    for u, d in zip(up, down, strict=True):
        if isnan(u) or isnan(d):
            out.append(NAN)
        elif d == 0:
            out.append(100.0)
        elif u == 0:
            out.append(0.0)
        else:
            out.append(100 - 100 / (1 + u / d))
    return out


def ref_stoch(c, h, low, length):
    out = [NAN] * len(c)
    for i in range(length - 1, len(c)):
        hh, ll = h[i - length + 1 : i + 1], low[i - length + 1 : i + 1]
        if any(isnan(v) for v in [*hh, *ll, c[i]]) or max(hh) == min(ll):
            continue
        out[i] = 100 * (c[i] - min(ll)) / (max(hh) - min(ll))
    return out


def ref_cci(xs, length):
    out = [NAN] * len(xs)
    for i in range(length - 1, len(xs)):
        w = xs[i - length + 1 : i + 1]
        if any(isnan(v) for v in w):
            continue
        mean = sum(w) / length
        mad = sum(abs(v - mean) for v in w) / length
        if mad > 0:
            out[i] = (xs[i] - mean) / (0.015 * mad)
    return out


def ref_stdev(xs, length):
    out = [NAN] * len(xs)
    for i in range(length - 1, len(xs)):
        w = xs[i - length + 1 : i + 1]
        if not any(isnan(v) for v in w):
            mean = sum(w) / length
            out[i] = math.sqrt(sum((v - mean) ** 2 for v in w) / length)
    return out


def ref_macd(xs, fast, slow, signal):
    f, s = ref_ema(xs, fast), ref_ema(xs, slow)
    macd = [NAN if isnan(a) or isnan(b) else a - b for a, b in zip(f, s, strict=True)]
    sig = ref_ema(macd, signal)
    hist = [NAN if isnan(a) or isnan(b) else a - b for a, b in zip(macd, sig, strict=True)]
    return macd, sig, hist


def ref_mom(xs, length):
    return [NAN] * length + [
        NAN if isnan(xs[i]) or isnan(xs[i - length]) else xs[i] - xs[i - length]
        for i in range(length, len(xs))
    ]


def ref_roc(xs, length):
    out = [NAN] * len(xs)
    for i in range(length, len(xs)):
        a, b = xs[i], xs[i - length]
        if not (isnan(a) or isnan(b)) and b != 0:
            out[i] = 100 * (a - b) / b
    return out


def hlc(n=300, seed=3, gaps=False):
    rng = random.Random(seed)
    c = [2000 + sum(rng.gauss(0, 3) for _ in range(i // 7 + 1)) for i in range(n)]
    h = [v + abs(rng.gauss(0, 2)) + 0.1 for v in c]
    low = [v - abs(rng.gauss(0, 2)) - 0.1 for v in c]
    if gaps:
        for i in (40, 41, 120):
            c[i] = NAN
        h[200] = NAN
    return h, low, c


# Wilder's RSI(14) example (the 33-close sample of StockCharts' RSI worksheet) and the 19
# values it prints, to two decimals, reproduced here from memory, not quoted from the source.
# All 19 were checked against the formula loop above before use: they agree, so each
# confirms the other. A regression check against the textbook method, not a citation.
WILDER_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
    45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
    46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
    43.4205, 42.6628, 43.1314,
]  # fmt: skip
WILDER_RSI = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]  # fmt: skip


def test_rsi_against_wilders_published_example():
    got = ta.rsi(S(WILDER_CLOSES), 14)
    assert got.iloc[:14].isna().all()  # the first value is at bar 14 (14 changes)
    assert [round(v, 2) for v in got.iloc[14:]] == WILDER_RSI


@pytest.mark.parametrize("length", [1, 2, 3, 14, 30])
@pytest.mark.parametrize("gaps", [False, True])
def test_rsi_matches_the_reference(length, gaps):
    _, _, c = hlc(gaps=gaps)
    same(ta.rsi(S(c), length), ref_rsi(c, length))


def test_rsi_by_hand_and_its_edges():
    # length 2, closes 1 2 1 2: changes +1 -1 +1. Seed at bar 2: up 0.5, down 0.5 -> 50;
    # bar 3: up (0.5 + 1) / 2 = 0.75, down 0.25 -> 100 - 100 / (1 + 3) = 75.
    same(ta.rsi(S([1, 2, 1, 2]), 2), [NAN, NAN, 50.0, 75.0])
    assert ta.rsi(S(range(1, 20)), 5).iloc[5:].eq(100).all()  # only gains
    assert ta.rsi(S(range(20, 1, -1)), 5).iloc[5:].eq(0).all()  # only losses
    # No change at all (0 / 0): 100, as in the reference implementation's `down == 0` case.
    assert ta.rsi(S([5.0] * 10), 3).iloc[3:].eq(100).all()


def test_tr_by_hand():
    h, low, c = S([10, 13, 11]), S([8, 12, 9]), S([9, 12.5, 10])
    # bar 0: high - low. Bar 1 gaps up: max(1, |13 - 9|, |12 - 9|) = 4. Bar 2: max(2, 1.5, 3.5).
    same(ta.tr(h, low, c), [2.0, 4.0, 3.5])


@pytest.mark.parametrize("length", [1, 2, 5, 14, 30])
@pytest.mark.parametrize("gaps", [False, True])
def test_tr_and_atr_match_the_references(length, gaps):
    h, low, c = hlc(gaps=gaps)
    same(ta.tr(S(h), S(low), S(c)), ref_tr(h, low, c))
    same(ta.atr(S(h), S(low), S(c), length), ref_atr(h, low, c, length))


def test_atr_by_hand_starts_from_the_first_bars_range():
    # True ranges 2, 3, 2 (the first is high - low). Length 2: seed (2 + 3) / 2 = 2.5 at
    # bar 1, then (2.5 * 1 + 2) / 2 = 2.25.
    h, low, c = S([10, 12, 11]), S([8, 9, 9]), S([9, 11, 10])
    same(ta.tr(h, low, c), [2.0, 3.0, 2.0])
    same(ta.atr(h, low, c, 2), [NAN, 2.5, 2.25])


@pytest.mark.parametrize("length", [1, 3, 14, 30])
@pytest.mark.parametrize("gaps", [False, True])
def test_stoch_cci_stdev_match_the_references(length, gaps):
    h, low, c = hlc(gaps=gaps)
    same(ta.stoch(S(c), S(h), S(low), length), ref_stoch(c, h, low, length))
    # Deviations of prices near 2000 lose a few digits to x - mean in both implementations.
    same(ta.cci(S(c), length), ref_cci(c, length), rel=1e-9)
    same(ta.stdev(S(c), length), ref_stdev(c, length), rel=1e-9)


def test_stoch_cci_stdev_by_hand():
    h, low, c = S([10, 12]), S([6, 8]), S([7, 11])
    same(ta.stoch(c, h, low, 2), [NAN, 100 * (11 - 6) / 6])  # (close - lowest low) / range
    flat = S([5.0, 5, 5])
    same(ta.stoch(flat, flat, flat, 2), [NAN] * 3)  # no range
    same(ta.cci(S([1, 2, 3]), 3), [NAN, NAN, 100.0])  # (3 - 2) / (0.015 * 2/3)
    same(ta.cci(S([4.0, 4, 4]), 3), [NAN] * 3)  # no deviation
    same(ta.cci(S([0.1, 0.1, 0.1]), 3), [NAN] * 3)  # also when the mean rounds (0.3 / 3 != 0.1)
    same(ta.cci(S([2000.1] * 5), 4), [NAN] * 5)
    same(ta.stdev(S([1, 2, 3]), 3), [NAN, NAN, math.sqrt(2 / 3)])  # population, not sample


def test_bb_by_hand_and_against_the_references():
    basis, upper, lower = ta.bb(S([1, 2, 3]), 3, 2.0)
    same(basis, [NAN, NAN, 2.0])
    same(upper, [NAN, NAN, 2 + 2 * math.sqrt(2 / 3)])
    same(lower, [NAN, NAN, 2 - 2 * math.sqrt(2 / 3)])
    assert ta.bb(S([1, 2, 3]), 3)[1].iloc[2] == pytest.approx(2 + 2 * math.sqrt(2 / 3))  # mult 2
    _, _, c = hlc(gaps=True)
    mid, dev = ref_sma(c, 20), ref_stdev(c, 20)
    b, u, lo = ta.bb(S(c), 20, 1.5)
    same(b, mid)
    same(u, [NAN if isnan(m) else m + 1.5 * d for m, d in zip(mid, dev, strict=True)], rel=1e-9)
    same(lo, [NAN if isnan(m) else m - 1.5 * d for m, d in zip(mid, dev, strict=True)], rel=1e-9)


@pytest.mark.parametrize(("fast", "slow", "signal"), [(12, 26, 9), (3, 5, 2), (1, 2, 1), (5, 3, 4)])
@pytest.mark.parametrize("gaps", [False, True])
def test_macd_matches_the_reference(fast, slow, signal, gaps):
    _, _, c = hlc(gaps=gaps)
    got = ta.macd(S(c), fast, slow, signal)
    for g, want in zip(got, ref_macd(c, fast, slow, signal), strict=True):
        same(g, want)


def test_macd_defaults_are_tradingviews_12_26_9():
    _, _, c = hlc()
    for got, want in zip(ta.macd(S(c)), ref_macd(c, 12, 26, 9), strict=True):
        same(got, want)
    assert ta.macd(S(c))[0].first_valid_index() == 25
    # Each one can still be given alone.
    for got, want in zip(ta.macd(S(c), slow=10), ref_macd(c, 12, 10, 9), strict=True):
        same(got, want)


def test_bb_default_multiplier_is_2():
    _, _, c = hlc()
    default, explicit = ta.bb(S(c), 20), ta.bb(S(c), 20, 2.0)
    for a, b in zip(default, explicit, strict=True):
        same(a, b.tolist())
    mid, dev = ref_sma(c, 20), ref_stdev(c, 20)
    same(
        default[1],
        [NAN if isnan(m) else m + 2 * d for m, d in zip(mid, dev, strict=True)],
        rel=1e-9,
    )
    same(
        default[2],
        [NAN if isnan(m) else m - 2 * d for m, d in zip(mid, dev, strict=True)],
        rel=1e-9,
    )


def test_macd_starts_where_its_parts_do_and_is_zero_on_a_constant():
    macd, signal, hist = ta.macd(S([5.0] * 60), 12, 26, 9)
    assert macd.first_valid_index() == 25  # the slow EMA's first bar
    assert signal.first_valid_index() == 25 + 9 - 1  # 9 MACD values later
    assert hist.first_valid_index() == signal.first_valid_index()
    assert macd.dropna().eq(0).all() and hist.dropna().eq(0).all()
    macd, signal, hist = ta.macd(S(range(60)), 3, 6, 4)
    assert (hist.dropna() == (macd - signal).dropna()).all()


def test_mom_and_roc():
    same(ta.mom(S([1, 3, 6]), 1), [NAN, 2.0, 3.0])
    same(ta.roc(S([2, 3, 6]), 1), [NAN, 50.0, 100.0])
    same(ta.roc(S([0, 1, 2]), 1), [NAN, NAN, 100.0])  # from zero is a gap, not infinity
    _, _, c = hlc(gaps=True)
    for n in (1, 4, 20):
        same(ta.mom(S(c), n), ref_mom(c, n))
        same(ta.roc(S(c), n), ref_roc(c, n))


GROUP2 = {
    "rsi": lambda x: ta.rsi(x, 6),
    "tr": lambda x: ta.tr(x + 1.5, x - 1.5, x),
    "atr": lambda x: ta.atr(x + 1.5, x - 1.5, x, 5),
    "stoch": lambda x: ta.stoch(x, x + 1.5, x - 1.5, 6),
    "cci": lambda x: ta.cci(x, 6),
    "stdev": lambda x: ta.stdev(x, 6),
    "mom": lambda x: ta.mom(x, 4),
    "roc": lambda x: ta.roc(x, 4),
    "macd": lambda x: ta.macd(x, 3, 6, 2)[0],
    "macd_signal": lambda x: ta.macd(x, 3, 6, 2)[1],
    "macd_hist": lambda x: ta.macd(x, 3, 6, 2)[2],
    "bb_basis": lambda x: ta.bb(x, 6)[0],
    "bb_upper": lambda x: ta.bb(x, 6, 1.5)[1],
    "bb_lower": lambda x: ta.bb(x, 6)[2],
}


@pytest.mark.parametrize("name", GROUP2)
def test_no_lookahead_gaps_short_inputs_and_the_index(name):
    fn = GROUP2[name]
    full = fn(X)
    for k in range(1, len(X) + 1):  # the value at bar k - 1 with and without what follows
        assert value_at(fn(X.iloc[:k]), k - 1) == pytest.approx(value_at(full, k - 1)), f"k={k}"
    changed = X.copy()
    changed.iloc[70:] = changed.iloc[70:] * 1.5 + 3
    other = fn(changed)
    for i in range(70):
        assert value_at(full, i) == pytest.approx(value_at(other, i)), f"bar {i}"
    for n in (0, 1, 2, 3):  # short and empty inputs
        assert len(fn(X.iloc[:n].reset_index(drop=True))) == n
    moved = X.copy()
    moved.index = pd.RangeIndex(1000, 1000 + len(X))
    assert fn(moved).index.equals(moved.index)


@pytest.mark.parametrize("bad", [0, -3, 2.5, "5", True])
def test_a_bad_length_is_refused(bad):
    one = S([1.0, 2.0])
    for call in (
        lambda: ta.rsi(one, bad),
        lambda: ta.cci(one, bad),
        lambda: ta.stdev(one, bad),
        lambda: ta.mom(one, bad),
        lambda: ta.roc(one, bad),
        lambda: ta.bb(one, bad),
        lambda: ta.atr(one + 1, one, one, bad),
        lambda: ta.stoch(one, one + 1, one, bad),
        lambda: ta.macd(one, bad, 5, 3),
        lambda: ta.macd(one, 2, bad, 3),
        lambda: ta.macd(one, 2, 5, bad),
    ):
        with pytest.raises(ValueError, match="length"):
            call()


def test_fewer_bars_than_the_length_is_all_gaps():
    short = S([1.0, 2.0, 3.0])
    for out in (
        ta.rsi(short, 10),
        ta.atr(short + 1, short, short, 10),
        ta.stoch(short, short + 1, short, 10),
        ta.cci(short, 10),
        ta.stdev(short, 10),
        *ta.bb(short, 10),
        *ta.macd(short, 3, 10, 2),
    ):
        assert out.isna().all() and len(out) == 3
