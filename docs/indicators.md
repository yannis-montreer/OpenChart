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

## Not yet

Multi-timeframe indicators (an H1 EMA on an M5 chart), alerts on indicators and the rest
of the first set (SMA, RSI, MACD, Bollinger Bands, ATR as files) come later; see
docs/decisions.md.
