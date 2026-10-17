"""The built-in indicators beyond EMA: SMA, RSI, MACD, Bollinger Bands, ATR, Stochastic and
the tick-volume histogram.

Each is compared with a reference written here as plain loops over Python floats, from
TradingView's definitions and not through `ta` (so a mistake in `ta` is not repeated), has
a no-lookahead test (the future replaced by noise changes nothing before it), and passes
the save-time checks, run as the worker runs a saved file.
"""

import math
import textwrap

import numpy as np
import pytest

from openchart.indicators._registry import BUILTIN_DIR, load_indicators
from openchart.indicators.worker.child import _compile
from tests.test_lookahead_checks import check, make_bars, run, warnings

IDS = ["sma", "rsi", "macd", "bollinger", "atr", "stochastic", "volume"]
NAN = float("nan")


def indicator(oid):
    return load_indicators().get(oid)


def compiled(oid):
    """The file as the worker compiles a saved one."""
    text = (BUILTIN_DIR / f"{oid}.py").read_text(encoding="utf-8")
    return _compile(f"{oid}.py", textwrap.dedent(text))


# References: plain loops --------------------------------------------------------------


def ref_sma(x, n):
    out = [NAN] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1 : i + 1]
        if not any(math.isnan(v) for v in w):
            out[i] = sum(w) / n
    return out


def ref_seeded(x, n, alpha):
    """TradingView's ema/rma: the first value is the average of the first n, then
    alpha * x + (1 - alpha) * previous. Leading gaps are skipped."""
    out = [NAN] * len(x)
    first = next((i for i, v in enumerate(x) if not math.isnan(v)), None)
    if first is None or first + n > len(x):
        return out
    seed = first + n - 1
    out[seed] = sum(x[first : seed + 1]) / n
    for i in range(seed + 1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def ref_ema(x, n):
    return ref_seeded(x, n, 2 / (n + 1))


def ref_rma(x, n):
    return ref_seeded(x, n, 1 / n)


def ref_rsi(x, n):
    up, down = [NAN], [NAN]
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        up.append(max(d, 0.0))
        down.append(max(-d, 0.0))
    u, d = ref_rma(up, n), ref_rma(down, n)
    out = []
    for a, b in zip(u, d, strict=True):
        if math.isnan(a) or math.isnan(b):
            out.append(NAN)
        elif b == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + a / b))
    return out


def ref_wma(x, n):
    out = [NAN] * len(x)  # the newest weighs n, down to 1
    for i in range(n - 1, len(x)):
        w = x[i - n + 1 : i + 1]
        out[i] = sum((k + 1) * v for k, v in enumerate(w)) / (n * (n + 1) / 2)
    return out


def ref_vwma(x, vol, n):
    out = [NAN] * len(x)
    for i in range(n - 1, len(x)):
        pv = sum(a * b for a, b in zip(x[i - n + 1 : i + 1], vol[i - n + 1 : i + 1], strict=True))
        v = sum(vol[i - n + 1 : i + 1])
        out[i] = pv / v if v > 0 else NAN
    return out


def ref_ma(kind, x, n, vol=None):
    return {
        "SMA": lambda: ref_sma(x, n),
        "EMA": lambda: ref_ema(x, n),
        "RMA": lambda: ref_rma(x, n),
        "WMA": lambda: ref_wma(x, n),
        "VWMA": lambda: ref_vwma(x, vol, n),
    }[kind]()


def ref_macd(x, fast, slow, signal, osc="EMA", sig_ma="EMA"):
    f, s = ref_ma(osc, x, fast), ref_ma(osc, x, slow)
    line = [a - b for a, b in zip(f, s, strict=True)]
    sig = ref_ma(sig_ma, line, signal)
    return line, sig, [a - b for a, b in zip(line, sig, strict=True)]


def ref_bands(x, n, mult, kind="SMA", vol=None):
    basis = ref_ma(kind, x, n, vol)
    upper, lower = [NAN] * len(x), [NAN] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1 : i + 1]
        m = sum(w) / n  # the deviation is always about the window's own simple mean
        dev = math.sqrt(sum((v - m) ** 2 for v in w) / n)  # population, as TradingView
        upper[i], lower[i] = basis[i] + mult * dev, basis[i] - mult * dev
    return basis, upper, lower


def ref_tr(h, low, c):
    out = [h[0] - low[0]]
    for i in range(1, len(c)):
        out.append(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])))
    return out


def ref_atr(h, low, c, n, smoothing):
    tr = ref_tr(h, low, c)
    if smoothing == "RMA":
        return ref_rma(tr, n)
    if smoothing == "SMA":
        return ref_sma(tr, n)
    if smoothing == "EMA":
        return ref_ema(tr, n)
    out = [NAN] * len(tr)  # WMA: the newest weighs n, down to 1
    for i in range(n - 1, len(tr)):
        w = tr[i - n + 1 : i + 1]
        out[i] = sum((k + 1) * v for k, v in enumerate(w)) / (n * (n + 1) / 2)
    return out


def ref_stoch(h, low, c, n, smooth_k, smooth_d):
    raw = [NAN] * len(c)
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1 : i + 1]), min(low[i - n + 1 : i + 1])
        if hh > ll:
            raw[i] = 100 * (c[i] - ll) / (hh - ll)
    k = ref_sma(raw, smooth_k)
    return k, ref_sma(k, smooth_d)


# Comparing ----------------------------------------------------------------------------


def same(got, want, tol=1e-9):
    got, want = np.asarray(got, dtype="float64"), np.asarray(want, dtype="float64")
    assert got.shape == want.shape
    assert (np.isnan(got) == np.isnan(want)).all(), "gaps are not in the same places"
    ok = ~np.isnan(want)
    scale = max(1.0, float(np.max(np.abs(want[ok]))) if ok.any() else 1.0)
    assert np.abs(got[ok] - want[ok]).max(initial=0.0) <= tol * scale


def computed(oid, frame, **params):
    ind = indicator(oid)
    return run(ind, frame, ind.normalize(params))


def columns(frame):
    return (frame[k].tolist() for k in ("o", "h", "l", "c", "v"))


def gapped(n, seed):
    """Bars that open away from the previous close (make_bars opens exactly on it, where the
    true range is always high - low and ATR could not tell the two apart)."""
    f = make_bars(n, seed=seed)
    rng = np.random.default_rng(seed + 1)
    c = f["c"].to_numpy()
    o = np.concatenate([[c[0]], c[:-1]]) + rng.normal(0, 2.5, n)
    f["o"] = o.round(3)
    f["h"] = (np.maximum(o, c) + rng.uniform(0, 0.6, n)).round(3)
    f["l"] = (np.minimum(o, c) - rng.uniform(0, 0.6, n)).round(3)
    return f


FRAME = gapped(900, seed=3)

# Reference comparisons ----------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 9, 50])
@pytest.mark.parametrize("src", ["close", "hl2"])
def test_sma(n, src):
    got = computed("sma", FRAME, length=n, source=src)
    x = FRAME["c"] if src == "close" else (FRAME["h"] + FRAME["l"]) / 2
    same(got["sma"], ref_sma(x.tolist(), n))


@pytest.mark.parametrize("n", [2, 14, 30])
def test_rsi(n):
    got = computed("rsi", FRAME, length=n)
    same(got["rsi"], ref_rsi(FRAME["c"].tolist(), n))


def test_rsi_stays_between_0_and_100_and_a_flat_run_is_100():
    flat = FRAME.copy()
    flat["c"] = 2000.0
    got = computed("rsi", flat, length=14)["rsi"]
    assert (got[14:] == 100.0).all()  # nothing moved: no loss in the window
    mid = computed("rsi", FRAME)["rsi"]
    assert np.nanmin(mid) >= 0 and np.nanmax(mid) <= 100


@pytest.mark.parametrize("fast,slow,signal", [(12, 26, 9), (5, 13, 4), (3, 6, 2)])
def test_macd(fast, slow, signal):
    got = computed("macd", FRAME, fast=fast, slow=slow, signal=signal)
    line, sig, hist = ref_macd(FRAME["c"].tolist(), fast, slow, signal)
    same(got["macd"], line)
    same(got["signal"], sig)
    same(got["hist"], hist)


@pytest.mark.parametrize(
    "osc,sig",
    [("EMA", "SMA"), ("SMA", "EMA"), ("SMA", "SMA")],
    ids=["osc-EMA-sig-SMA", "osc-SMA-sig-EMA", "both-SMA"],
)
def test_macd_moving_average_types(osc, sig):
    got = computed("macd", FRAME, osc_ma=osc, signal_ma=sig)
    line, signal, hist = ref_macd(FRAME["c"].tolist(), 12, 26, 9, osc, sig)
    same(got["macd"], line)
    same(got["signal"], signal)
    same(got["hist"], hist)


@pytest.mark.parametrize("kind", ["SMA", "EMA", "RMA", "WMA", "VWMA"])
@pytest.mark.parametrize("n", [5, 20])
def test_bollinger_basis_types(kind, n):
    got = computed("bollinger", FRAME, length=n, ma_type=kind)
    basis, upper, lower = ref_bands(FRAME["c"].tolist(), n, 2.0, kind, FRAME["v"].tolist())
    same(got["basis"], basis)
    same(got["upper"], upper)
    same(got["lower"], lower)


def test_bollinger_vwma_without_any_volume_is_a_gap():
    frame = FRAME.copy()
    frame["v"] = 0.0
    got = computed("bollinger", frame, ma_type="VWMA")
    assert np.isnan(got["basis"]).all() and np.isnan(got["upper"]).all()


@pytest.mark.parametrize("n", [1, 5, 20])
def test_volume_moving_average(n):
    got = computed("volume", FRAME, show_ma=True, ma_length=n)
    same(got["ma"], ref_sma(FRAME["v"].tolist(), n))
    same(got["volume"], FRAME["v"].tolist())


def test_volume_moving_average_is_off_by_default_and_then_all_gaps():
    got = computed("volume", FRAME)
    assert np.isnan(got["ma"]).all()
    assert np.isnan(computed("volume", FRAME, ma_length=5)["ma"]).all()  # length alone: nothing


@pytest.mark.parametrize("n,mult", [(20, 2.0), (5, 1.0), (30, 2.5)])
def test_bollinger(n, mult):
    got = computed("bollinger", FRAME, length=n, mult=mult)
    basis, upper, lower = ref_bands(FRAME["c"].tolist(), n, mult)
    same(got["basis"], basis)
    same(got["upper"], upper)
    same(got["lower"], lower)


@pytest.mark.parametrize("smoothing", ["RMA", "SMA", "EMA", "WMA"])
@pytest.mark.parametrize("n", [3, 14])
def test_atr(n, smoothing):
    got = computed("atr", FRAME, length=n, smoothing=smoothing)
    _, h, low, c, _ = columns(FRAME)
    same(got["atr"], ref_atr(h, low, c, n, smoothing))


@pytest.mark.parametrize("n,sk,sd", [(14, 1, 3), (5, 3, 3), (10, 1, 1)])
def test_stochastic(n, sk, sd):
    got = computed("stochastic", FRAME, k_length=n, k_smooth=sk, d_smooth=sd)
    _, h, low, c, _ = columns(FRAME)
    k, d = ref_stoch(h, low, c, n, sk, sd)
    same(got["k"], k)
    same(got["d"], d)


def test_stochastic_is_between_0_and_100():
    got = computed("stochastic", FRAME)
    assert np.nanmin(got["k"]) >= 0 and np.nanmax(got["k"]) <= 100


def test_volume_is_the_tick_count_as_it_is():
    got = computed("volume", FRAME)
    same(got["volume"], FRAME["v"].tolist())


# Schema: inputs, defaults and output kinds --------------------------------------------


def described(oid):
    return indicator(oid).to_json()


def defaults(oid):
    return {p["name"]: p["default"] for p in described(oid)["params"]}


def kinds(oid):
    return [o["kind"] for o in described(oid)["outputs"]]


def test_all_of_them_load_next_to_ema():
    registry = load_indicators()
    assert registry.errors == []
    assert sorted(registry.indicators) == sorted(["ema", *IDS])


def test_inputs_and_defaults_are_tradingviews():
    expected = {
        "sma": {"length": 9, "source": "close"},
        "rsi": {
            "length": 14,
            "source": "close",
            "overbought": 70,
            "middle": 50,
            "oversold": 30,
        },
        "macd": {
            "fast": 12,
            "slow": 26,
            "signal": 9,
            "source": "close",
            "osc_ma": "EMA",
            "signal_ma": "EMA",
        },
        "bollinger": {"length": 20, "mult": 2.0, "source": "close", "ma_type": "SMA"},
        "atr": {"length": 14, "smoothing": "RMA"},
        "stochastic": {
            "k_length": 14,
            "k_smooth": 1,
            "d_smooth": 3,
            "overbought": 80,
            "middle": 50,
            "oversold": 20,
        },
        "volume": {"show_ma": False, "ma_length": 20},
    }
    for oid, want in expected.items():
        inputs = {
            p["name"]: p["default"] for p in described(oid)["params"] if p["group"] == "inputs"
        }
        assert inputs == want, oid


def test_output_kinds_and_panes():
    assert kinds("sma") == ["line"] and described("sma")["overlay"] is True
    assert kinds("rsi") == ["line", "level", "level", "level"]
    assert kinds("macd") == ["histogram", "line", "line"]
    assert kinds("bollinger") == ["line", "line", "line", "fill"]
    assert described("bollinger")["overlay"] is True
    assert kinds("atr") == ["line"]
    assert kinds("stochastic") == ["line", "line", "level", "level", "level"]
    assert kinds("volume") == ["histogram", "line"]
    for oid in ("rsi", "macd", "atr", "stochastic", "volume"):
        assert described(oid)["overlay"] is False, oid


def test_levels_follow_their_inputs_and_the_fill_is_between_the_bands():
    levels = [o for o in described("rsi")["outputs"] if o["kind"] == "level"]
    # The middle level comes last, so the ids of the two older levels do not move.
    assert [o["input"] for o in levels] == ["overbought", "oversold", "middle"]
    assert [o["label"] for o in levels] == ["Overbought", "Oversold", "Middle"]
    assert all(o["style"] == "dashed" for o in levels)
    levels = [o for o in described("stochastic")["outputs"] if o["kind"] == "level"]
    assert [o["input"] for o in levels] == ["overbought", "oversold", "middle"]
    assert all(o["style"] == "dashed" for o in levels)
    out = described("bollinger")["outputs"]
    assert out[-1]["between"] == [out[1]["name"], out[2]["name"]]
    assert [o["label"] for o in out[:3]] == ["Basis", "Upper", "Lower"]
    assert [o["label"] for o in described("macd")["outputs"]] == ["Histogram", "MACD", "Signal"]
    assert [o["label"] for o in described("stochastic")["outputs"][:2]] == ["%K", "%D"]


def test_volume_says_it_is_tick_volume():
    d = described("volume")
    assert d["outputs"][0]["label"] == "Tick volume"
    assert d["outputs"][1]["label"] == "Tick volume MA"
    assert "tick" in d["title"].lower()


def test_the_new_choices_offer_exactly_the_helpers_that_exist():
    def choices(oid, name):
        return next(p["choices"] for p in described(oid)["params"] if p["name"] == name)

    assert choices("macd", "osc_ma") == choices("macd", "signal_ma") == ["EMA", "SMA"]
    assert choices("bollinger", "ma_type") == ["SMA", "EMA", "RMA", "WMA", "VWMA"]


def test_the_levels_are_bounded_numbers():
    for oid in ("rsi", "stochastic"):
        ind = indicator(oid)
        with pytest.raises(ValueError):
            ind.normalize({"middle": 101})
        with pytest.raises(ValueError):
            ind.normalize({"middle": -1})
        with pytest.raises(ValueError):
            ind.normalize({"overbought": 101})
        with pytest.raises(ValueError):
            ind.normalize({"oversold": -1})


# No lookahead -------------------------------------------------------------------------

VARIANTS = {
    "sma": [{}, {"length": 30, "source": "hlc3"}],
    "rsi": [{}, {"length": 5}],
    "macd": [
        {},
        {"fast": 5, "slow": 9, "signal": 3},
        {"osc_ma": "SMA"},
        {"signal_ma": "SMA"},
        {"osc_ma": "SMA", "signal_ma": "SMA"},
    ],
    "bollinger": [{}, {"length": 8, "mult": 1.5}]
    + [{"ma_type": k} for k in ("EMA", "RMA", "WMA", "VWMA")],
    "atr": [{}, {"length": 5, "smoothing": "WMA"}, {"smoothing": "SMA"}, {"smoothing": "EMA"}],
    "stochastic": [{}, {"k_length": 5, "k_smooth": 3, "d_smooth": 2}],
    "volume": [{}, {"show_ma": True}, {"show_ma": True, "ma_length": 200}],
}
CASES = [(oid, p) for oid, ps in VARIANTS.items() for p in ps]
CASE_IDS = [
    f"{oid}-{'default' if not p else '-'.join(f'{k}={v}' for k, v in p.items())}"
    for oid, p in CASES
]


@pytest.mark.parametrize("oid,params", CASES, ids=CASE_IDS)
def test_nothing_before_a_bar_depends_on_the_bars_after_it(oid, params):
    ind = indicator(oid)
    p = ind.normalize(params)
    base = run(ind, FRAME, p)
    rng = np.random.default_rng(11)
    for cut in (120, 400, 777):
        noisy = FRAME.copy()
        tail = len(noisy) - cut - 1
        for col in ("o", "h", "l", "c", "v"):
            noisy.loc[cut + 1 :, col] = rng.uniform(1, 5000, tail)
        other = run(ind, noisy, p)
        for name in base:
            a, b = base[name][: cut + 1], other[name][: cut + 1]
            assert (np.isnan(a) == np.isnan(b)).all(), (oid, name, cut)
            assert (a[~np.isnan(a)] == b[~np.isnan(b)]).all(), (oid, name, cut)


@pytest.mark.parametrize("oid,params", CASES, ids=CASE_IDS)
def test_fewer_bars_at_the_end_give_the_same_values(oid, params):
    ind = indicator(oid)
    p = ind.normalize(params)
    full = run(ind, FRAME, p)
    part = run(ind, FRAME.iloc[:-60], p)
    for name in full:
        same(part[name], full[name][: len(FRAME) - 60], tol=1e-12)


# The save-time checks say ok ----------------------------------------------------------

LONG = gapped(4000, seed=5)


@pytest.mark.parametrize("oid,params", CASES, ids=CASE_IDS)
def test_the_save_time_checks_find_nothing(oid, params):
    ind = compiled(oid)
    plan, ahead, behind = check(ind, LONG, params)
    assert plan.cut and plan.start is not None, "both checks must run, not be skipped"
    assert ahead == []
    assert warnings(behind) == []
    assert behind == []


@pytest.mark.parametrize("oid", IDS)
def test_every_file_declares_a_warmup(oid):
    ind = indicator(oid)
    p = ind.normalize({})
    if oid == "volume":
        assert ind.warmup(p) == 0
    else:
        assert ind.warmup(p) >= 1


def test_warmups_grow_with_the_lengths():
    assert indicator("rsi").warmup(indicator("rsi").normalize({"length": 28})) > indicator(
        "rsi"
    ).warmup(indicator("rsi").normalize({"length": 14}))
    macd = indicator("macd")
    assert macd.warmup(macd.normalize({"slow": 52})) > macd.warmup(macd.normalize({}))
    atr = indicator("atr")
    assert atr.warmup(atr.normalize({"smoothing": "RMA"})) > atr.warmup(
        atr.normalize({"smoothing": "SMA"})
    )


def test_a_gap_in_the_prices_is_a_gap_not_an_error():
    frame = FRAME.copy()
    frame.loc[300:303, ["o", "h", "l", "c"]] = np.nan
    for oid in IDS:
        got = computed(oid, frame)
        for name, values in got.items():
            assert len(values) == len(frame), (oid, name)
