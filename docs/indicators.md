# Writing an indicator

An indicator is one Python file. The server lists the files in the Indicators menu; the
settings dialog (Inputs and Style tabs) is built from what the file declares, so a new file
needs no change to the web interface.

Values are computed on the server's machine, on the same bars the chart shows (the same
timeframe and price type: mid, bid or ask), and drawn aligned to the candles. Built-in
indicators run in the server; your own files run in a separate worker process with less
than the server can do (no tokens, and on a service or Docker install no data folder and no
network): see [custom-indicators.md](custom-indicators.md).

## Where the files go

- **Your own indicators:** `.py` files in the `indicators` folder of the data folder
  (`<data dir>/indicators/`, for example `data/indicators/` next to the database). The
  Indicators menu shows the exact path. The server creates the folder on start.
- **Built-in ones:** `src/openchart/indicators/` (`ema.py`, `sma.py`, `rsi.py`, `macd.py`, `bollinger.py`, `atr.py`, `stochastic.py`, `volume.py`; see "Built-in indicators" below).

A new or changed file is picked up without a restart (its modification time or size
changed). Files whose name starts with `_` are skipped. The file name is the indicator's id
(lowercase letters, digits and `_`), at most 256 KB. Your files and the built-in ones are
separate: a file of yours named `ema.py` no longer replaces the built-in EMA (the menu shows
the built-in one until it gets its Built-in and Custom sections).

A file that fails to load (a syntax error, a missing declaration) does not stop anything:
it is listed under "Files that did not load" in the Indicators menu with the reason and its
line. A script that raises shows "Mid failed: ZeroDivisionError: division by zero (line
11)"; one that hangs is stopped at its deadline (5 s); one that fails that way 3 times in a
row is stopped until you change the file. Inside the worker, scripts cannot open files,
use the network or start programs ("custom indicators cannot open files"); `print()` goes
to the server log.

## What a file declares

```python
"""Distance of the close from its EMA, in its own pane."""

from openchart.indicators import Color, Int, Output, Source, Width, ema, source

NAME = "Dist"  # short: the legend shows "Dist 20 close"
TITLE = "Distance from EMA"  # the Indicators menu
OVERLAY = False  # True: on the price pane; False: a pane of its own

PARAMS = [
    Int("length", 20, min=1, max=1000),
    Source("source", "close"),
    Color("color", "#f59e0b", label="Colour"),
    Width("width", 2),
]

OUTPUTS = [Output("dist", "Distance", color="color", width="width")]


def warmup(params):
    """How many bars before the first one shown are needed for settled values."""
    return 10 * params["length"]


def compute(bars, params):
    src = source(bars, params["source"])
    return {"dist": src - ema(src, params["length"])}
```

- `NAME`, `TITLE`, `OVERLAY`, `PARAMS`, `OUTPUTS` and `compute` are required; `warmup` is
  optional (default 0).
- `compute(bars, params)` gets a pandas DataFrame with columns `ts, o, h, l, c, v` (oldest
  first, bar start times in UTC seconds; also under the long names `time, open, high, low,
  close, volume`, so `bars.close` reads naturally; volume is OANDA's tick count) and every
  parameter, validated, with defaults filled in. It returns `{output name: values}`, one
  value per bar; NaN where there is no value yet. It may return pandas Series, numpy arrays
  or lists.
- **`compute(bars, params, info)`:** with a third argument it also gets `info.instrument`
  (`"XAU_USD"`), `info.display_name` (`"Gold"`), `info.decimals`, `info.timeframe` (`"1h"`)
  and `info.price` (`"mid"`, `"bid"` or `"ask"`). `info` is read-only. A two-argument
  `compute` is called as before.
- `warmup(params)` asks for extra bars before the ones on screen, so the first value shown
  is the same as if it had been computed over the whole history. Without it, a moving
  average starts over at the left edge of each request.

## Parameters

| Helper | Field in the dialog | Tab |
|---|---|---|
| `Int(name, default, min=, max=, label=)` | number | Inputs |
| `Float(name, default, min=, max=, step=, label=)` | number | Inputs |
| `Bool(name, default, label=)` | checkbox | Inputs |
| `Choice(name, default, ["a", "b"], label=)` | dropdown | Inputs |
| `String(name, default, max_length=100, label=)` | text box (at most `max_length` characters, 1 to 1000) | Inputs |
| `Source(name="source", default="close", label=)` | dropdown: open, high, low, close, hl2, hlc3, ohlc4, hlcc4 | Inputs |
| `Color(name, default, label=)` | colour picker (`#rrggbb`) | Style |
| `Width(name, default=1, label=)` | line width, 1 to 4 px | Style |

The label defaults to the name, capitalised. Values are checked on the server (type, min,
max, choices) before `compute` runs, and in the dialog before they are applied. The
legend shows the name followed by the Inputs values ("EMA 9 close").

## Outputs

Colour and width are the **names of `Color` and `Width` inputs**, so the Style tab can change
them. An output's `label` is what the legend shows.

| Declaration | Draws | `compute` returns |
|---|---|---|
| `Line(name, label, color=, width=, style="solid")` (`Output` is the M7 name for it) | a line; `style` is `solid`, `dashed` or `dotted` | a number per bar, NaN = gap |
| `Histogram(name, label, color=, base=0)` | columns from `base` | a number per bar |
| `Area(name, label, color=, width=None)` | a line with a shaded area below it | a number per bar |
| `Fill(between=("upper", "lower"), color=, opacity=0.15, label="Fill")` | the band between two `Line`/`Area` outputs | nothing |
| `Level(value, label="Level", color=, width=None, style="dashed")` | a horizontal line; `value` is a number or the name of a `Float` input | nothing |
| `Marker(name, label, color=, shape="circle", position="above", text="")` | a shape on the bars that are True; `shape` is `arrow_up`, `arrow_down`, `circle`, `square`; `position` is `above`, `below`, `on` (the close) | True/False per bar |
| `Background(name, label, color=, opacity=0.15)` | the bar's background, tinted, where True | True/False per bar |

`Fill` and `Level` have no name and take no values; `compute` must not return them.
Markers and backgrounds need real booleans (a comparison, `ta.crossover(...)`), not 0/1 and not
NaN. Lines, histograms and areas need numbers, not booleans; infinities count as gaps. A line
that is NaN everywhere is allowed (it just draws nothing: usually "not enough bars yet").

## What is checked

**When the file loads,** each failure names the file, the input or output and the rule
(`mine.py: output 'fast': 'width' is not a width parameter`):

- input and output names are identifiers (letters, digits, `_`; at most 40 characters) and
  unique among inputs, and among outputs (an input and an output may share a name);
- every default passes its own type's check; `min <= max`; a `Choice` has choices, without
  repeats, and its default is one of them; a `String` default fits `max_length`;
- an output's colour is a `Color` input and its width a `Width` input; a `Fill` names two
  different `Line`/`Area` outputs; a `Level`'s input is a `Float`;
- at most 30 inputs and 16 outputs, labels (and `NAME`, `TITLE`, a marker's `text`) at most 60
  characters, the file at most 256 KB.

**Every time `compute` runs,** the values are checked before anything is drawn, by the worker
and again by the server: every declared value output is present, nothing undeclared is
returned, each has one value per bar, the right type for its kind. A bad result is an error
shown for that indicator ("SMA cross failed: ResultError: output 'up' must be true/false
values..."), never a crash; it does not count toward the quarantine.

## Building blocks

`openchart.indicators` provides, besides the parameter helpers:

- `source(bars, name)`: the source price of each bar
- `sma(values, length)`: simple moving average
- `ema(values, length)`: exponential moving average, alpha = 2 / (length + 1), seeded with
  the simple average of the first `length` values like TradingView's `ta.ema`

They take and return series, so they compose: `ema(sma(source(bars, "close"), 5), 9)`.
Leading NaN (another indicator's warm-up) are skipped before an EMA is seeded.

### `ta`: the helper library

`from openchart.indicators import ta`. Helpers take series (or lists) and return a series
with the same index. The value at a bar uses that bar and earlier ones only. Not enough
bars, or a gap in the window, gives NaN (False for the true/false helpers), never an error.
Written from the public formulas; TradingView's documented definitions, including how each
one starts.

| Helper | What it returns | Needs (warmup) |
|---|---|---|
| `sma(x, n)` | simple average of the last `n` | `n` bars |
| `ema(x, n)` | alpha 2/(n+1), seeded with the SMA of the first `n` | `n` bars for the first value; about `10 * n` more to agree with a longer history to 1e-8 |
| `rma(x, n)` | Wilder's, alpha 1/n, seeded the same way | `n` bars; about `20 * n` to agree to 1e-8 |
| `wma(x, n)` | newest weighs `n`, down to 1 | `n` bars |
| `vwma(x, volume, n)` | sum(x * volume) / sum(volume); NaN if no volume (volume is OANDA's tick count) | `n` bars |
| `hma(x, n)` | `wma(2 * wma(x, n // 2) - wma(x, n), round(sqrt(n)))` | `n + round(sqrt(n)) - 1` bars |
| `shift(x, n)` | the value `n` bars ago (Pine's `x[n]`); `n >= 1` only, a zero or negative offset is refused | `n` bars |
| `change(x, n=1)` | `x - shift(x, n)` | `n` bars |
| `nz(x, r=0)` | gaps replaced by `r` | none |
| `crossover(a, b)`, `crossunder`, `cross` | `a > b` now and `a <= b` on the bar before; either side may be a number (`crossover(rsi, 30)`) | one bar after the inputs' own |
| `barssince(cond)` | bars since `cond` was True (0 on a True bar); NaN before the first | until `cond` has been True once (it counts from the first bar it sees) |
| `valuewhen(cond, x, n=0)` | `x` on the bar where `cond` was True the `n`-th time back | until `cond` has been True `n + 1` times |
| `rising(x, n)`, `falling(x, n)` | each of the last `n` steps went up (down): `x[0] > x[1] > ... > x[n]` | `n + 1` bars |
| `rsi(x, n)` | `100 - 100 / (1 + up / down)`, `up`/`down` the `rma` of gains/losses; 100 with no loss in the window (also when nothing moved), 0 with no gain | first value at bar `n`; about `20 * n` to agree to 1e-8 |
| `tr(high, low, close)` | greatest of `high - low`, `abs(high - previous close)`, `abs(low - previous close)`; the first bar (or a missing previous close) is `high - low` | none |
| `atr(high, low, close, n)` | `rma(tr, n)`, seeded with the average of the first `n` true ranges | first value at bar `n - 1`; about `20 * n` to agree to 1e-8 |
| `stoch(close, high, low, n)` | raw %K: `100 * (close - lowest low) / (highest high - lowest low)` over the last `n` bars; a window with no range is a gap. Smooth with `sma` | `n` bars |
| `cci(x, n)` | `(x - sma) / (0.015 * mean absolute deviation)`; no deviation is a gap | `n` bars |
| `macd(x, fast=12, slow=26, signal=9)` | `(macd, signal, hist)`: `ema(fast) - ema(slow)`, `ema` of that, and their difference | MACD from bar `max(fast, slow) - 1`, signal `signal - 1` bars later; about `10 * (slow + signal)` to agree to 1e-8 |
| `mom(x, n)`, `roc(x, n)` | `x - x[n]`; `100 * (x - x[n]) / x[n]` (from zero is a gap) | first value at bar `n` |
| `stdev(x, n)` | population deviation (divided by `n`), TradingView's default | `n` bars |
| `bb(x, n, mult=2.0)` | `(basis, upper, lower)`: `sma` and `mult` population deviations either side | `n` bars |
| `highest(x, n)`, `lowest(x, n)` | extreme of the last `n` bars, the current one included | `n` bars |
| `highestbars(x, n)`, `lowestbars(x, n)` | where that extreme is, as Pine: `0` this bar, `-1` the bar before, down to `-(n - 1)`; on a tie the most recent bar | `n` bars |
| `sum(x, n)` | sum of the last `n` | `n` bars |
| `cum(x)` | running total from the first bar; a gap is a gap on its own bar and the total goes on | **no warmup helps**: it depends on where the history starts |
| `pivothigh(x, left, right)`, `pivotlow` | the pivot's price on the bar where it is *confirmed*, `right` bars after the pivot, NaN elsewhere. A pivot high is at least as high as the `left` bars before it and higher than the `right` bars after it | `left + right + 1` bars |

A gap in the input: `ema` and `rma` give NaN at the gap and start again, with a new
seed, once `n` values in a row are back; windowed helpers (including `highest`, `lowest`,
`highestbars`, `lowestbars`, `sum` and the pivots) give NaN while the gap is in the window.
`rsi`, `atr` and `macd` are built on `ema`/`rma`, so a gap acts the same way.

**Pivots appear late, on purpose.** A pivot high needs `right` lower bars after it, so it
cannot be known before the bar that is `right` bars later. `pivothigh` puts its value on that
bar, so a marker or line drawn from it shows up `right` bars after the turn, and the value
at a bar never changes when later bars arrive. Drawing it back at the pivot bar
itself would need the future, so `ta` does not offer it.

**Equal prices in a pivot** (as TradingView, checked for `pivothigh`): bars on the left may
equal the pivot, bars on the right must be strictly lower. Of a run of equal highs the last
one is the pivot, and an equal high on the right means no pivot. `pivotlow` is the mirror
image (that part is inferred, not yet checked).

Speed (this PC, 5,000 bars): the recursive and series helpers take 0.1 to 1.2 ms;
`highest`/`lowest` 0.2 ms at any length. The rest cost bars x length: at length 20 they are
0.2 to 0.4 ms (`wma`, `vwma`, `hma`, `cci`, `stdev`, `bb`, `sum`, `highestbars`,
`pivothigh` alike) and at length 2000 about 10 to 30 ms (`sum` 11, `pivothigh` 17,
`highestbars` 30).

## Built-in indicators

Written with the same `ta` helpers a file of yours uses, and with TradingView's inputs and
defaults, so they are also the examples to copy (`src/openchart/indicators/`). Each declares a
`warmup` and passes the save-time lookahead and warmup checks; `tests/test_builtin_indicators.py`
compares each with a reference written as plain loops.

| Id | Pane | Inputs (defaults) | Draws |
|---|---|---|---|
| `ema` | price | length 9, source close | line |
| `sma` | price | length 9 (1 to 5000), source close | line |
| `rsi` | own | length 14 (1 to 900), source close, overbought 70, oversold 30, middle 50 | line, a dashed level at each of the three |
| `macd` | own | fast 12, slow 26, signal 9 (each 1 to 900), source close, oscillator MA type EMA, signal MA type EMA (EMA or SMA) | histogram, MACD line, signal line |
| `bollinger` | price | length 20 (1 to 900), source close, StdDev 2.0, basis MA type SMA (SMA, EMA, RMA, WMA or VWMA) | basis, upper and lower lines, a fill between the bands |
| `atr` | own | length 14 (1 to 900), smoothing RMA (or SMA, EMA, WMA) | line |
| `stochastic` | own | %K length 14, %K smoothing 1, %D smoothing 3, overbought 80, oversold 20, middle 50 | %K and %D lines, a dashed level at each of the three |
| `volume` | own | show moving average off, moving average length 20 | histogram labelled **Tick volume**, and a line "Tick volume MA" |

- **`volume` is tick volume.** OANDA gives no real volume: each bar's value is the number of
  price updates in it, so it shows activity, not traded size, and differs from a volume
  indicator on a venue with real volume. The label says so.
- **Moving-average types** are the `ta` helpers that exist: the Bollinger basis offers SMA,
  EMA, RMA, WMA and VWMA (the weighted-by-volume one uses the tick volume; a window with no
  volume is a gap); the MACD's oscillator and signal offer EMA and SMA. The bands are always
  `StdDev` population deviations about the window's simple mean, whatever the basis, as in
  TradingView. The defaults give the values the indicators gave before the options existed
  (`tests/test_builtin_defaults.py` holds them).
- **The middle level** (50 on RSI and Stochastic) is a third dashed level, an input like the
  other two.
- **The volume average** is a simple average of the tick volume, off by default. The
  "Tick volume MA" line is always declared (outputs are fixed), so while it is off it is a
  line with no values.
- **Lengths stop at 900** for RSI, MACD, Bollinger and ATR (5000 for SMA, whose window is
  its whole warmup): the settled warmup of the recursive averages is about 21 lengths, and the
  values route fetches at most 20,000 bars before the first one shown. A warmup beyond that
  (these lengths if they were allowed, the built-in EMA above length 1999, a file of yours that
  declares a large `warmup`) is cut, and the answer says so: a `notice` (and `checks.warmup_cut`
  for your files), shown as a small "Warmup cut" warning on the legend row (docs/api.md, "Warmup
  notice"). Stochastic and volume stay within the limit at their maximums.
- **What is not there (yet):** the Bollinger `offset`, and a histogram that changes colour
  with its sign (an output has one colour; the style tab sets it). Prices are assumed to have
  `high >= low`; bars that break that (never from OANDA) can put %K outside 0 to 100.
- **Level inputs** are ordinary `Float` inputs (0 to 100): change them in the Inputs tab and
  the lines move.
- **Warmups** (bars before the first one shown): `sma`, `stochastic`, `volume`'s average, ATR
  with SMA or WMA and Bollinger with SMA, WMA or VWMA need only their window (the sum of the
  lengths for `stochastic`); the recursive ones fade: `rsi` and ATR with RMA about
  `21 * length`, ATR with EMA and Bollinger with EMA `11 * length`, Bollinger with RMA
  `21 * length`, `macd` `11 * slow` for an EMA oscillator and `11 * signal` for an EMA signal
  (their window for SMA).

## Not yet

Multi-timeframe indicators (an H1 EMA on an M5 chart) and alerts on indicators come later;
see docs/decisions.md. The four examples in [custom-indicators.md](custom-indicators.md) show
how to write them as files today.
