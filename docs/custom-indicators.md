# Custom indicators

Your own indicators are Python files in `<data dir>/indicators/`. This page is the guide to
writing them (four working examples, every output kind, a cheat sheet for people coming from
Pine Script), then what the save-time checks tell you, and then where your code runs, how
isolated that is, and when you may edit it in the browser. The file format itself, the
`Int`/`Float`/`Color`... inputs and every `ta` helper with its warmup are in
[indicators.md](indicators.md); the design is in `custom-indicators-design.md`.

Status: the worker and its isolation (CI-1), and the `ta` helpers, output kinds, drawing and
checks (CI-2), and per-chart indicator lists kept on the server with the menu's Built-in and Your files sections (CI-3). The editor in the browser comes in CI-4.

## Where to start

1. Copy one of the four examples below into `<data dir>/indicators/` (the Indicators menu
   shows the exact path) under a name of lowercase letters, digits and `_`, for example
   `my_cross.py`. Files in this repository are in `docs/examples/indicators/`.
2. It shows up in the Indicators menu within a moment; no restart. A file that does not load
   is listed there with the reason and the line.
3. Change it, save, and the chart updates. A script that raises shows "Could not compute" in the
   legend (hover for the error and its line) and draws nothing; the rest of the chart is not
   affected.

What you can use: everything in `openchart.indicators` (inputs, outputs, `source`, `ta`),
plus `numpy` and `pandas`, and plain Python loops over the bars. What you cannot: files, the
network, other programs, other instruments or timeframes (see "Where your code runs").
`print()` goes to the server log.

Two rules make an indicator behave: **declare a `warmup`** (how many earlier bars a value
needs; without it each request starts over at its left edge, and the warmup check will say so),
and **never use a later bar than the one you are computing** (the lookahead check looks for
that; below).

## Four examples

Each one is a real file, run in the test suite against the real worker on fake bars, and
passes both save-time checks with no findings. The page shows them exactly as they are.

### SMA cross: lines and markers on the price

Two averages, and an arrow where they cross. `crossover(a, b)` is true on the bar where `a`
moved above `b`. The arrows are `Marker` outputs, which take a true/false value per bar.
Colours and the width are inputs, so each can be changed in the Style tab.

<!-- example: sma_cross.py -->
```python
"""SMA cross: two moving averages and a marker where they cross."""

from openchart.indicators import Color, Int, Line, Marker, Source, Width, source, ta

NAME = "SMA cross"
TITLE = "SMA cross"
OVERLAY = True

PARAMS = [
    Int("fast", 10, min=1, max=500),
    Int("slow", 30, min=2, max=2000),
    Source("source", "close"),
    Color("fast_color", "#2962ff", label="Fast colour"),
    Color("slow_color", "#ff6d00", label="Slow colour"),
    Width("width", 1),
    Color("up_color", "#26a69a", label="Up colour"),
    Color("down_color", "#ef5350", label="Down colour"),
]
OUTPUTS = [
    Line("fast", "Fast", color="fast_color", width="width"),
    Line("slow", "Slow", color="slow_color", width="width"),
    Marker("up", "Cross up", color="up_color", shape="arrow_up", position="below"),
    Marker("down", "Cross down", color="down_color", shape="arrow_down", position="above"),
]


def warmup(params):
    # A cross needs both averages on the bar before it, so one bar more than the slow one.
    return params["slow"] + 1


def compute(bars, params):
    src = source(bars, params["source"])
    fast, slow = ta.sma(src, params["fast"]), ta.sma(src, params["slow"])
    return {
        "fast": fast,
        "slow": slow,
        "up": ta.crossover(fast, slow),
        "down": ta.crossunder(fast, slow),
    }
```

### RSI with bands: its own pane, levels you can change, tinted bars

`OVERLAY = False` puts it in a pane of its own. `Level("upper", ...)` draws a horizontal line
at the value of the `Float` input of that name, so changing Overbought in Settings moves the
line and the tint with it. `Background` tints the bars where its value is true, and a marker
draws in the pane too.

<!-- example: rsi_bands.py -->
```python
"""RSI with bands: the line, overbought and oversold levels you can change, a tint while
it is beyond them, and an arrow when it comes back inside."""

from openchart.indicators import (
    Background,
    Color,
    Float,
    Int,
    Level,
    Line,
    Marker,
    Source,
    Width,
    source,
    ta,
)

NAME = "RSI bands"
TITLE = "RSI with overbought and oversold bands"
OVERLAY = False

PARAMS = [
    Int("length", 14, min=2, max=200),
    Float("upper", 70, min=50, max=100, label="Overbought"),
    Float("lower", 30, min=0, max=50, label="Oversold"),
    Source("source", "close"),
    Color("rsi_color", "#7e57c2", label="RSI colour"),
    Width("width", 2),
    Color("high_color", "#ef5350", label="Overbought colour"),
    Color("low_color", "#26a69a", label="Oversold colour"),
    Color("mid_color", "#787b86", label="Middle colour"),
]
OUTPUTS = [
    Line("rsi", "RSI", color="rsi_color", width="width"),
    Level("upper", "Overbought", color="high_color"),
    Level(50, "Middle", color="mid_color", style="dotted"),
    Level("lower", "Oversold", color="low_color"),
    Background("hot", "Overbought", color="high_color", opacity=0.12),
    Background("cold", "Oversold", color="low_color", opacity=0.12),
    Marker("back_down", "Back inside", color="high_color", shape="arrow_down", position="above"),
    Marker("back_up", "Back inside", color="low_color", shape="arrow_up", position="below"),
]


def warmup(params):
    # RSI is seeded with an average and then smoothed: about 20 lengths agree to 1e-8.
    return 20 * params["length"]


def compute(bars, params):
    rsi = ta.rsi(source(bars, params["source"]), params["length"])
    return {
        "rsi": rsi,
        "hot": rsi >= params["upper"],
        "cold": rsi <= params["lower"],
        "back_down": ta.crossunder(rsi, params["upper"]),
        "back_up": ta.crossover(rsi, params["lower"]),
    }
```

### Bollinger-style bands: a fill between two lines

`ta.bb` returns the middle and the two edges. `Fill(between=("upper", "lower"), ...)` shades
between two outputs that have values; it takes no values itself, so `compute` does not return
it. A bar where either edge has no value (the first `length - 1` bars) is left unshaded.

<!-- example: bollinger_fill.py -->
```python
"""Bollinger-style bands: a moving average, a band of `mult` deviations around it, the band
shaded, and the bars that close outside it tinted."""

from openchart.indicators import (
    Background,
    Color,
    Fill,
    Float,
    Int,
    Line,
    Source,
    Width,
    source,
    ta,
)

NAME = "Bands"
TITLE = "Bollinger-style bands"
OVERLAY = True

PARAMS = [
    Int("length", 20, min=2, max=500),
    Float("mult", 2.0, min=0.1, max=10, label="Deviations"),
    Source("source", "close"),
    Color("mid_color", "#2962ff", label="Middle colour"),
    Color("band_color", "#ff6d00", label="Band colour"),
    Width("width", 1),
]
OUTPUTS = [
    Line("basis", "Middle", color="mid_color", width="width"),
    Line("upper", "Upper", color="band_color", width="width", style="dashed"),
    Line("lower", "Lower", color="band_color", width="width", style="dashed"),
    Fill(between=("upper", "lower"), color="band_color", opacity=0.1, label="Band"),
    Background("outside", "Close outside", color="band_color", opacity=0.1),
]


def warmup(params):
    return params["length"]


def compute(bars, params):
    src = source(bars, params["source"])
    basis, upper, lower = ta.bb(src, params["length"], params["mult"])
    return {
        "basis": basis,
        "upper": upper,
        "lower": lower,
        "outside": (src > upper) | (src < lower),
    }
```

### A marker on a pattern: engulfing candles

For a pattern, write the condition as a comparison of columns and of the same columns one bar
back (`ta.shift(x, 1)`; a plain `x.shift(1)` is the same). The result must be real true/false
values for every bar, with no gaps: a comparison against a missing first bar is `False`, and
`fillna(False)` makes sure. A marker's `text` is drawn next to the shape.

<!-- example: engulfing.py -->
```python
"""A marker on a pattern: an engulfing candle.

A bullish engulfing bar closes up, after a down bar, with a body that covers the whole body
of that bar. A bearish one is the mirror image. Only the bar and the one before it are read,
so there is nothing to look ahead to and no warmup beyond that one bar.
"""

from openchart.indicators import Color, Marker, ta

NAME = "Engulfing"
TITLE = "Engulfing candles"
OVERLAY = True

PARAMS = [
    Color("bull_color", "#26a69a", label="Bullish colour"),
    Color("bear_color", "#ef5350", label="Bearish colour"),
]
OUTPUTS = [
    Marker("bull", "Bullish", color="bull_color", shape="arrow_up", position="below", text="E"),
    Marker("bear", "Bearish", color="bear_color", shape="arrow_down", position="above", text="E"),
]


def warmup(params):
    return 1


def compute(bars, params):
    o, c = bars["open"], bars["close"]
    prev_o, prev_c = ta.shift(o, 1), ta.shift(c, 1)
    bull = (c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)
    bear = (c < o) & (prev_c > prev_o) & (c <= prev_o) & (o >= prev_c)
    # Gaps (the first bar) compare False, which is what a marker needs: never NaN.
    return {"bull": bull.fillna(False).astype(bool), "bear": bear.fillna(False).astype(bool)}
```

## Output kinds

An output is declared in `OUTPUTS`, and `compute` returns a value for each one that has values.
Colour and width name `Color` and `Width` inputs (so the Style tab can change them).

| Declaration | Draws | `compute` returns |
|---|---|---|
| `Line(name, label, color=, width=, style="solid")` | a line (`solid`, `dashed`, `dotted`); breaks where there is no value | a number per bar, NaN for none |
| `Histogram(name, label, color=, base=0)` | columns from `base` | a number per bar |
| `Area(name, label, color=, width=None)` | a line with a shaded area below it | a number per bar |
| `Fill(between=("upper", "lower"), color=, opacity=0.15)` | the band between two `Line` or `Area` outputs | nothing |
| `Level(value, label, color=, width=None, style="dashed")` | a horizontal line at a number, or at a `Float` input's value (give its name) | nothing |
| `Marker(name, label, color=, shape=, position=, text="")` | `arrow_up`, `arrow_down`, `circle` or `square`, `above`, `below` or `on` the bar | true/false per bar |
| `Background(name, label, color=, opacity=0.15)` | the bar's column, tinted | true/false per bar |

- **Panes:** `OVERLAY = True` draws on the price pane, `False` in a pane of its own. Markers and
  backgrounds work in both. In a pane of its own a marker sits against the value of the first output that
  has values, on that bar; on the price pane against the bar's high (above), low (below) or close (on).
- **Hits are sparse.** The server sends a marker or background as the list of bars where it is
  true, not a value for every bar, so a marker every tenth bar costs a tenth.
- **What `compute` may return:** markers and backgrounds need real booleans, not 0/1 and not NaN;
  lines, histograms and areas need numbers, not booleans (an infinity counts as a gap). Anything
  else is an error shown for that indicator.
- **Not supported yet:** a colour that changes per bar (use two outputs: one for each colour),
  a line drawn into the past, text labels and boxes other than a marker's `text`.

## From Pine Script

Rough rules: a Pine script runs bar by bar; a file here runs once over all the bars as columns
(pandas Series), so `if` becomes a comparison or `np.where`, and `x[1]` becomes
`ta.shift(x, 1)`. The helpers are written from the public formulas and follow TradingView's
documented definitions, including how each one starts. Where something differs from Pine, the row
or the list below it says so.

| Pine | Here |
|---|---|
| `//@version`, `indicator("Name", overlay=true)` | `NAME`, `TITLE`, `OVERLAY = True` |
| `input.int`, `input.float`, `input.bool` | `Int`, `Float`, `Bool` |
| `input.string(options=[...])` | `Choice`; free text is `String` |
| `input.source` | `Source` (open, high, low, close, hl2, hlc3, ohlc4, hlcc4) |
| `input.color`, the width in `plot()` | `Color`, `Width` |
| `close`, `open`, `high`, `low`, `volume`, `hl2`... | `bars.close`..., `source(bars, "hl2")`; volume is OANDA's tick count |
| `x[1]`, `x[n]` | `ta.shift(x, n)`; only `n >= 1` (no future) |
| `na`, `na(x)`, `nz(x, 0)`, `fixnan(x)` | `np.nan`, `x.isna()`, `ta.nz(x, 0)`, `x.ffill()` |
| `plot(x)` | a `Line` output (`style=`: `plot.style_line`); histogram style: `Histogram`; area style: `Area` |
| `hline(70)` | `Level(70, ...)` |
| `fill(p1, p2)` | `Fill(between=("p1", "p2"), ...)` |
| `bgcolor(cond ? color : na)` | `Background` |
| `plotshape(cond, style=shape.triangleup, location=location.belowbar)` | `Marker(shape="arrow_up", position="below")`; `plotchar` and its text: `text=` |
| `color.new(c, 80)` | the output's `opacity` (fills and backgrounds) |
| `syminfo.ticker`, `timeframe.period` | `info.instrument`, `info.timeframe` (a third argument to `compute`) |
| `ta.sma`, `ta.ema`, `ta.rma`, `ta.wma`, `ta.vwma`, `ta.hma` | `ta.sma(x, n)`, `ta.ema`, `ta.rma`, `ta.wma`, `ta.vwma(x, volume, n)`, `ta.hma` |
| `ta.rsi`, `ta.cci`, `ta.mom`, `ta.roc` | `ta.rsi(x, n)`, `ta.cci(x, n)`, `ta.mom(x, n)`, `ta.roc(x, n)` |
| `ta.macd(x, 12, 26, 9)` | `macd, signal, hist = ta.macd(x, 12, 26, 9)` |
| `ta.stoch(close, high, low, n)` | `ta.stoch(close, high, low, n)`: the raw %K, as in Pine; smooth it with `ta.sma` |
| `ta.bb(x, n, mult)` | `basis, upper, lower = ta.bb(x, n, mult)` |
| `ta.tr`, `ta.atr(n)` | `ta.tr(high, low, close)`, `ta.atr(high, low, close, n)` |
| `ta.stdev(x, n)` | `ta.stdev(x, n)` (population deviation, as Pine) |
| `ta.highest(x, n)`, `ta.lowest`, `ta.highestbars`, `ta.lowestbars` | the same names; the bar offsets are 0, -1, -2 ... as in Pine |
| `math.sum(x, n)`, `ta.cum(x)` | `ta.sum(x, n)`, `ta.cum(x)` (`ta.cum` depends on where the history starts: no warmup can fix it) |
| `ta.change(x, n)` | `ta.change(x, n)` |
| `ta.crossover(a, b)`, `ta.crossunder`, `ta.cross` | the same; either side may be a number |
| `ta.rising(x, n)`, `ta.falling(x, n)` | the same |
| `ta.barssince(cond)`, `ta.valuewhen(cond, x, n)` | the same |
| `ta.pivothigh(x, left, right)`, `ta.pivotlow` | the same, with the value on the bar that **confirms** the pivot, `right` bars after it, as Pine reports it. Pine scripts then often `plot(..., offset=-right)` to draw it back at the pivot: that is the future and is not available (see below) |
| `if`/`else`, ternaries | `np.where(cond, a, b)`, `a.where(cond, b)`, boolean masks |
| `var` state, loops over bars | an ordinary Python loop over the arrays (5,000 bars take a few milliseconds) |
| `math.abs`, `math.log`, `math.sqrt`... | `numpy` (`np.abs`, `np.log`, `np.sqrt`) |

**No equivalent** (say so rather than guess):

- **Anything that looks ahead:** a negative `offset` in `plot`, `x[-1]`, `request.security` with
  lookahead, `bar_index` arithmetic that depends on how many bars are loaded (a loop index here
  also depends on where the window starts: use the warmup and `ta` helpers instead).
- **Other timeframes and symbols:** `request.security`, `request.*`. Multi-timeframe indicators
  are deferred.
- **Alerts and trading:** `alert()`, `alertcondition()`, `strategy.*`. Alerts on indicators are
  queued; drawn-line alerts exist today.
- **Real-time state:** `barstate.isrealtime`, `barstate.isconfirmed`, `barstate.islast`. Values on
  the newest bar are recomputed on every refresh and can change until it closes.
- **Drawing objects:** `label.*`, `line.*`, `box.*`, `table.*`, `polyline.*`. A `Marker` has a
  short text; there are no free-form labels.
- **A colour per bar** (`color=cond ? a : b`): use two outputs.
- **Helpers `ta` does not have:** `ta.alma`, `ta.swma`, `ta.linreg`, `ta.median`,
  `ta.percentrank`, `ta.variance`, `ta.correlation`, `ta.sar`, `ta.supertrend`, `ta.kc`,
  `ta.dmi`, `ta.mfi`, `ta.obv`, `ta.vwap`, `ta.tsi`, `ta.wpr`, `ta.cmo`, `ta.cog`, `ta.bbw`,
  `ta.kcw`. Most are a few lines of pandas (`x.rolling(n).median()`, `x.rolling(n).corr(y)`);
  write the loop yourself where a smoothing is recursive. Whatever you write is yours to check:
  the save-time checks below are what tells you if it peeks.
- **Pine's `bar_index`, `time`, `timenow`, `last_bar_index`:** `bars.time` is each bar's start
  (UTC seconds); the others have no use in a file that runs over the bars once.

## What the save-time checks tell you

Where you see them: until the editor (CI-4) shows them next to the code, the answer is in the API,
as `checks` in `GET /api/indicators/{id}` and in each custom entry of `GET /api/indicators`
(docs/api.md). A warning is about your file with the settings and bars that were checked; fix the
cause, or declare a longer `warmup`.

When a chart first asks for a new version of your file with some settings, the worker
runs the script twice more on the same bars, in the background, and the server compares
the answers. **They warn; they never block.** The file runs as before, and what the checks
found is in `checks` (docs/api.md).

- **Lookahead:** the same bars without the last 50 (a quarter of them on short charts).
  Every value both runs share must be equal; if one differs, the script reads later bars
  (`shift(-1)`, a centred window, `bfill`, `bars[i + 1]`, the mean of the whole series, the
  last bar or the bar count), or it does not give the same result twice (random numbers,
  the clock).
- **Warmup:** the same bars starting `warmup` bars later (at least 50). Past that run's
  own warmup the values must agree with the full run's. If they do not, the warning says
  how many bars of warmup would have been enough. If they never agree, the output depends
  on every earlier bar (`ta.cum`, a running total) and no warmup can fix it.
- **Equal means:** a gap (NaN or an infinity) equals a gap; numbers within 1e-9
  (lookahead) or 1e-7 (warmup) of the output's largest value; markers and backgrounds
  exactly. A marker that differs only where every line agrees is shown as a note: two
  values that are equal to within rounding (a near-tie), not a short warmup. A file with
  only markers cannot be told apart, so there it stays a warning that mentions near-ties.

**The checks find some lookahead, not all.** A passing check does not mean a script is
free of lookahead. They do not see:

- lookahead inside the warmup (`ta.sma(c, 20).bfill()` fills the first 19 bars from bar 19);
- a read of later bars that the last 50 bars do not change (`c / c.max()` when the highest
  close is earlier);
- a branch on the bar count that both runs take the same way (`if len(bars) > 100`);
- a true/false output whose lookahead does not change it on these bars;
- in the warmup check, a marker built from a series that is not an output (an internal
  average with too short a warmup) while every declared line agrees: it can show up only
  as a near-tie note, not a warning;
- anything that only shows with other settings, another instrument or other data.

**What they cost:** at most 5 settings per version of a file are checked (later ones are
"not checked for these settings"). Each run is its own job at the lowest priority: a chart
waits for at most one of them, one check runs at a time, 1 s apart, and none within 10 s
of the worker starting. A run that would not fit in 80% of the deadline is skipped and
says so; one that runs past the deadline restarts the worker like any other, but is
reported, not counted toward the 3 failures that stop a file.

**Checking the checker** (development): `python tools/mutate_checks.py` breaks one rule at a
time in a copy of `src/` and needs the checks' tests to fail on each; it refuses to run
unless those tests pass on the unbroken code, and exits non-zero if a mutant survives.

## What the worker is

The rest of this page is about the process that runs your files and what it can and cannot reach.

- **One long-lived process**, started in the background when the server starts (1 to 3 s,
  mostly importing pandas). It is sent each script as source text and the bars as arrays,
  never a path, and answers with the values.
- **Limits:** 512 MB of memory, half of one CPU core, 5 s per computation
  (`OPENCHART_WORKER_MEMORY_MB`, `OPENCHART_WORKER_CPU_PERCENT`, `OPENCHART_WORKER_TIMEOUT_S`,
  1 to 30 s). A script past its deadline is killed with the worker, which restarts in 1 to
  2 s; a script that fails that way 3 times in a row is stopped until its file changes.
  A script's own exception is shown with its line and kept: the same request gets it back
  without running again, and a script that raised after more than half its deadline is
  not run again with those settings for 30 s. Editing the file runs it at once.
- **The rest of the server never waits on it:** the price feed, candles, alerts and the
  watchdog carry on while a script hangs (tested).
- **A clean environment:** no `OPENCHART_*`, no tokens.
- **A courtesy, not a wall:** an audit hook refuses files, sockets, programs and `ctypes`
  with a readable message, so honest mistakes fail clearly. Python's audit hooks are not a
  sandbox; the operating system's limits below are.

## How isolated it is, by install

| Install | Worker | Minimum isolation | Browser editing |
|---|---|---|---|
| Windows service, release zip | AppContainer + job object + a private desktop | met | per the setting below |
| Windows service, sandbox not set up (or a source checkout) | none | — | off; custom indicators do not run (built-in ones do); rerun the installer |
| Source install, `openchart run` | restricted token (Low integrity) + job object | not met | always off |
| Release zip run by hand (not the service) | restricted token + job object | not met | always off |
| Docker with `docker-compose.yml` | the `indicator-worker` container | met | per the setting below |
| Docker without compose, Linux, macOS | a child process, same user | not met | always off |

**The minimum isolation**, each point proven by a probe when the worker starts (the server
log lists them: `indicator sandbox probes: ...`):

1. no secrets in its environment;
2. it cannot read the data folder, the database, `secrets.enc` or the key file;
3. no network: not 127.0.0.1, not the LAN, not the internet;
4. it cannot open the server process;
5. it cannot start programs (Windows; in Docker it counts against 32 processes);
6. its limits hold, and it dies when the server dies.

Below the minimum, files in the folder still run, and the Indicators menu says
**"weaker isolation: this install cannot isolate custom code from your data and tokens"**.
On a source install the worker can still read your files, your Credential Manager entries
(the OANDA token) and use the network. That is less than a file of yours could do before
CI-1, when it ran inside the server, but it is not isolation.

The server log says which one this install got: `indicator sandbox: ready (AppContainer)`,
`ready (sidecar)`, `ready (restricted token); weaker isolation ...`, or
`indicator sandbox: unavailable: <reason>`. `openchart sandbox check` starts a worker the
same way, prints the probes and runs a script.

## Editing in the browser (from CI-4)

Editing custom code in a browser needs **both** of:

- the minimum isolation (above); no setting overrides it;
- the setting **`on`**, which only the server's own machine can change:

  ```
  openchart settings set indicator-editing on
  openchart settings set indicator-editing off
  openchart settings set indicator-editing auto   # the default: off
  ```

  (`--data-dir C:\ProgramData\OpenChart\data` for the service.) It works while the server
  runs and takes effect on the next request; no web page or API can change it. The
  Indicators menu shows whether editing is allowed and, if not, why and how to allow it.

**`auto` never allows editing.** Until the security review it meant "from the browser on
the server's own machine", told by a request arriving from 127.0.0.1 with no proxy header.
But anything that forwards a port on the PC (an SSH tunnel, `socat`, a tunnel agent) makes
a remote browser's request arrive from 127.0.0.1 the same way, so the rule could not keep
the phone, or someone with your password and such a tunnel, out. With `on`, every browser
that is logged in may edit, the phone included; turn it on when you edit and `off`
afterwards if you prefer.

The login and the same-origin check apply as for every change.

## What it does not protect against

- **A determined attacker** with an operating-system or kernel exploit. A container is not
  a virtual machine, and an AppContainer is not either.
- **A script that returns plausible but false values.**
- **Your custom indicators are not isolated from each other.** They all run in one worker
  process: one of them can change what another shows (its values, even its answers), slow
  it down or make it fail. The isolation is between them and your data, tokens and
  network, not between one indicator and the next. Only put files there you would run.
- **On an install below the minimum, almost anything a program running as you can do.**

Treat a custom indicator like a program you run. That is why browser editing is off unless
the install isolates code and you allow it from the server's machine.
