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
- **Built-in ones:** `src/openchart/indicators/` (for now only `ema.py`).

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
  first, bar start times in UTC seconds) and every parameter, validated, with defaults
  filled in. It returns `{output name: values}`, one value per bar; NaN where there is no
  value yet. It may return pandas Series, numpy arrays or lists.
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
| `Source(name="source", default="close", label=)` | dropdown: open, high, low, close, hl2, hlc3, ohlc4, hlcc4 | Inputs |
| `Color(name, default, label=)` | colour picker (`#rrggbb`) | Style |
| `Width(name, default=1, label=)` | line width, 1 to 4 px | Style |

The label defaults to the name, capitalised. Values are checked on the server (type, min,
max, choices) before `compute` runs, and in the dialog before they are applied. The
legend shows the name followed by the Inputs values ("EMA 9 close").

Each `Output` is one line, drawn with the colour and width parameters it names.

## Building blocks

`openchart.indicators` provides, besides the parameter helpers:

- `source(bars, name)`: the source price of each bar
- `sma(values, length)`: simple moving average
- `ema(values, length)`: exponential moving average, alpha = 2 / (length + 1), seeded with
  the simple average of the first `length` values like TradingView's `ta.ema`

They take and return series, so they compose: `ema(sma(source(bars, "close"), 5), 9)`.
Leading NaN (another indicator's warm-up) are skipped before an EMA is seeded.

### `ta`: the helper library (in progress, CI-2 step a)

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
| `barssince(cond)` | bars since `cond` was True (0 on a True bar); NaN before the first | none (it counts from the first bar it sees) |
| `valuewhen(cond, x, n=0)` | `x` on the bar where `cond` was True the `n`-th time back | until `cond` has been True `n + 1` times |
| `rising(x, n)`, `falling(x, n)` | each of the last `n` steps went up (down): `x[0] > x[1] > ... > x[n]` | `n + 1` bars |
| `rsi(x, n)` | `100 - 100 / (1 + up / down)`, `up`/`down` the `rma` of gains/losses; 100 with no loss in the window (also when nothing moved), 0 with no gain | first value at bar `n`; about `20 * n` to agree to 1e-8 |
| `tr(high, low, close)` | greatest of `high - low`, `abs(high - previous close)`, `abs(low - previous close)`; the first bar (or a missing previous close) is `high - low` | none |
| `atr(high, low, close, n)` | `rma(tr, n)`, seeded with the average of the first `n` true ranges | first value at bar `n - 1`; about `20 * n` to agree to 1e-8 |
| `stoch(close, high, low, n)` | raw %K: `100 * (close - lowest low) / (highest high - lowest low)` over the last `n` bars; a window with no range is a gap. Smooth with `sma` | `n` bars |
| `cci(x, n)` | `(x - sma) / (0.015 * mean absolute deviation)`; no deviation is a gap | `n` bars |
| `macd(x, fast, slow, signal)` | `(macd, signal, hist)`: `ema(fast) - ema(slow)`, `ema` of that, and their difference | MACD from bar `max(fast, slow) - 1`, signal `signal - 1` bars later; about `10 * (slow + signal)` to agree to 1e-8 |
| `mom(x, n)`, `roc(x, n)` | `x - x[n]`; `100 * (x - x[n]) / x[n]` (from zero is a gap) | first value at bar `n` |
| `stdev(x, n)` | population deviation (divided by `n`), TradingView's default | `n` bars |
| `bb(x, n, mult=2.0)` | `(basis, upper, lower)`: `sma` and `mult` population deviations either side | `n` bars |

A gap in the input: `ema` and `rma` give NaN at the gap and start again, with a new
seed, once `n` values in a row are back; windowed helpers give NaN while the gap is in
the window. `rsi`, `atr` and `macd` are built on them, so a gap acts the same way.
The rest of the first set (windows and pivots: `highest`, `lowest`, `highestbars`,
`lowestbars`, `sum`, `cum`, `pivothigh`, `pivotlow`) follows in the next step.

Speed (this PC, 5,000 bars): the recursive and series helpers take 0.1 to 1.2 ms. The
window helpers cost bars x length: `wma`, `vwma`, `hma`, `cci`, `stdev` and `bb` are 0.4 to
3 ms at length 20 to 200 and about 10 to 30 ms at length 2000.

## Not yet

Multi-timeframe indicators (an H1 EMA on an M5 chart), alerts on indicators and the rest
of the first set (SMA, RSI, MACD, Bollinger Bands, ATR as files) come later; see
docs/decisions.md.
