# Custom indicators: design proposal

Status: **approved** on 2026-10-15, with the changes below, and built: CI-0 to CI-5 are
merged (see `docs/roadmap.md`). The spike report is `docs/ci0-spike-report.md`.

Decisions and changes:

- **D1:** approved. Instances are kept per chart on the server, and the `localStorage`
  list is migrated the first time a chart is opened.
- **D2:** approved, with a change. The editing setting stays server-side only. The `auto`
  rule is to be revisited, because the owner's service binds 0.0.0.0 for Tailscale; a
  recommendation is in the spike report. Built in CI-1 (#17).
- **D3 to D6:** approved.
- **No ACL changes** to the owner's venv or base Python, ever.
  - **Source installs** use the restricted-token worker, with a visible "weaker isolation"
    notice.
  - **AppContainer** is for the service install only, set up by the installer.
  - Section 3.2's `openchart indicator-sandbox setup` for source installs is dropped.
- **Migration numbers** are picked at merge time: the next free one. (Storage step 1,
  0007, merged first in #13; CI-3 took 0008 and storage step 2 took 0009.)
- **A TradingView export** for the comparison tests is optional, belongs to the owner, and
  is never committed.
- **Browser editing stays off below the minimum isolation**, with no override. The
  definition is in the spike report.
- **D7 (a)**, 2026-10-15: if the AppContainer cannot be set up on a service install,
  custom indicators do not run at all, folder files included. The Custom section says why
  and to rerun the installer, and built-in indicators keep working.
- **CI-1** is approved as a plan (`docs/ci1-plan.md`): findings 1 to 10, plus the D2
  request-origin rule with the header check, the settings command and the behind-proxy
  setting. It starts after the owner's M8 tests and data move.

Goal: you write an indicator in Python, keep it on the server, and add it to any chart
(any instrument) from the Indicators menu, in a "Custom" section.

## 0. What exists today (M7), and three facts that shape the design

M7 gave us one indicator mechanism. This proposal extends it; it does not add a second one.

- **One `.py` file per indicator.** Each file declares `NAME`, `TITLE`, `OVERLAY`, `PARAMS`
  (`Int`, `Float`, `Bool`, `Choice`, `Source`, `Color`, `Width`) and `OUTPUTS` (lines
  only). It has `compute(bars, params)` and an optional `warmup(params)`.
- **Where the files live:**
  - built-in files in `src/openchart/indicators/` (only `ema.py` today);
  - your own files in `<data dir>/indicators/`.
- **The UI is generated from the schemas.** The settings dialog (Inputs and Style tabs) is
  built from what each file declares.
- **Values come from the server.** `GET /api/indicators/{id}` computes on the chart's bars
  plus the warmup bars. Today the browser asks for every bar it holds, in pages of 5,000,
  and refreshes the last two values once a second while a bar forms.

Three facts matter here:

1. **Your files already run inside the server process.** The server imports them and runs
   `compute` in a thread. That thread sits next to the broker tokens, the decrypted
   secrets and the alert engine.
   - A file that hangs ties up a thread forever. One that eats memory can take the server
     down.
   - That was acceptable while only you, with access to the PC, could add a file. Once
     code can come from the browser, it is not. **This proposal moves every
     non-built-in file into the worker, including the ones in the folder.**
2. **Under the service, a child process would run as LocalSystem.** It could read
   `secret.key`, `secrets.enc` and the database directly.
   - Run from the source checkout, it would run as you. It could read the database, find
     the `instance_id` and ask Credential Manager for `OpenChart/<instance_id>`, which is
     your OANDA token.
   - So a clean environment is not enough. **The worker must also run with fewer rights
     than the server.** Section 3 covers this.
3. **Indicators are not per instrument today.** The chart's list lives in the browser
   (`localStorage`) and in saved layouts. It follows you when you switch from gold to
   EURUSD, and each browser has its own list. Your brief says instances belong to a chart
   (source + instrument). That is a change; decision **D1** in section 9 covers it.

SPEC section 11 says indicator files are "never uploaded through the UI". This feature
changes that rule, under the conditions in section 3.6. The SPEC gets updated in the first
milestone.

## 1. Data model

### 1.1 Definitions: files in `<data dir>/indicators/`

There is one source of truth, the folder:

```
<data dir>/indicators/
  sma_cross.py                     the current version (what runs)
  rsi_bands.py
  .history/sma_cross/
    20261014T101502Z.py            the last 10 saved versions, oldest dropped
    20261014T102233Z.py
  .deleted/
    old_idea-20261014T110000Z/     deleted definitions, with their history
```

- **The id is the file name** (`sma_cross`), as today: lowercase letters, digits and `_`,
  at most 40 characters. It is fixed when the definition is created and never changes,
  so instances never point at a name that has moved.
- **What the menu shows** is `TITLE` and the legend `NAME`, both inside the code.
  - "Rename" edits the `TITLE = "..."` line. Templates always write it on one line. If a
    hand-written file has it in another form, Rename says "edit TITLE in the code".
  - Because the id does not change, nothing else needs updating.
- **Duplicate** copies the file under a new id (`sma_cross_2`) and title ("SMA cross
  (copy)").
- **History:** every save from the browser first copies the current file into
  `.history/<id>/`. The editor's "Versions" list can restore any of the last 10. A
  restore is itself a save, so it can be undone too.
- **Edits made outside the browser** (a text editor, git) are picked up without a
  restart. The server checks each file's modification time and size when it lists the
  catalog and before it computes. Today a restart is needed.
- **Delete** moves the file and its history into `.deleted/`. It can be restored by hand,
  and nothing is permanently erased. Section 5.4 covers deleting a definition that is in
  use.
- **Backups:** `openchart backup` already copies the indicators folder. `.history` and
  `.deleted` go with it.
- **Built-in indicators** stay in the package (`ema.py`, more later). They form the menu's
  "Built-in" section.
  - Custom and built-in ids are now separate namespaces. A file of yours named `ema.py` no
    longer replaces the built-in EMA; both show, one in each section.
  - Each layout entry gains `kind: "builtin" | "custom"`. Old entries are read as built-in,
    or as custom when only a file of yours has that id.

### 1.2 Instances: per chart, on the server (if D1 is approved)

A new table, migration `0007` on main (it gets renumbered if storage step 1 lands first):

```sql
CREATE TABLE chart_indicators (
  source      TEXT NOT NULL,
  instrument  TEXT NOT NULL,
  position    INTEGER NOT NULL,          -- order in the legend / panes
  uid         TEXT NOT NULL,             -- stable instance id
  kind        TEXT NOT NULL,             -- builtin | custom
  indicator   TEXT NOT NULL,             -- the definition id
  params      TEXT NOT NULL,             -- JSON, validated against the definition
  visible     INTEGER NOT NULL DEFAULT 1,
  price_labels INTEGER NOT NULL DEFAULT 1,
  legend_values INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (source, instrument, uid)
);
```

- **Like drawings:** a chart's indicators are the same in every browser and on the phone,
  and change when you switch instruments.
- **Layouts:** a saved layout keeps a copy of its instrument's instances, with their
  values. Loading the layout puts that copy on the chart. "Per-instance settings are saved
  in layouts" is then exactly true.
- **Parameter values that no longer fit an edited definition**, because an input was
  removed or a choice renamed:
  - they fall back to the default for that input only;
  - the legend shows a small "settings reset" note once.
- **Migration from today:** the first time a browser with an indicator list in
  `localStorage` opens a chart that has no stored instances, it copies its list onto that
  chart.

## 2. Authoring: the file format, extended

The file format stays the same as M7. Everything below is added to it, and all existing
files keep working.

```python
"""SMA cross: two moving averages and a marker where they cross."""

from openchart.indicators import Color, Int, Line, Marker, Source, Width, source, ta

NAME = "SMA×"
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
```

### 2.1 Inputs

The existing inputs are `Int`, `Float`, `Bool`, `Choice`, `Source`, `Color` and `Width`.

- **New `String(name, default, max_length=100)`**: free text, for example a label.
- **The schema is validated at load:**
  - names are identifiers and unique;
  - defaults pass their own check;
  - `min <= max`, and choices are not empty;
  - at most 30 inputs and 16 outputs;
  - labels are at most 60 characters, and a file is at most 256 KB.
  - Every rule gets a test.

### 2.2 What `compute` receives

- **`bars`**: the DataFrame from today, oldest first, in the chart's price type (mid, bid
  or ask), with the columns `ts, o, h, l, c, v`.
  - The long names `time, open, high, low, close, volume` are added as aliases, so
    `bars.close` reads naturally.
  - Volume is OANDA's tick count, and the docs say so.
- **`params`**: every input, validated, with defaults filled in. This is unchanged.
- **`info`**, optional: if `compute` declares a third argument, it gets `info.instrument`
  ("XAU_USD"), `info.display_name`, `info.decimals`, `info.timeframe` ("1h") and
  `info.price` ("mid").
- **Nothing else.** There is no access to other instruments or timeframes (multi-timeframe
  stays deferred), and no database or network. In the worker, bars arrive as arrays, so
  there is simply nothing else to reach. Section 3 covers what the OS enforces.

### 2.3 Outputs

| Declaration | Draws | Values `compute` returns |
|---|---|---|
| `Line(name, label, color=, width=, style="solid")` (today's `Output`, kept as an alias) | a line | float per bar, NaN = gap |
| `Histogram(name, label, color=, base=0)` | columns from `base` | float per bar |
| `Area(name, label, color=)` | a line with a shaded area below it | float per bar |
| `Fill(between=("upper", "lower"), color=, opacity=0.15)` | the band between two other outputs | nothing (it uses the two outputs) |
| `Level(value, label, color=, style="dashed")`, where `value` is a number or a `Float` input's name | a horizontal line | nothing |
| `Marker(name, label, color=, shape=, position=, text="")` | a shape on bars | bool per bar (True = draw) |
| `Background(name, label, color=, opacity=0.15)` | the bar's background column, tinted | bool per bar |

- **Colour and width:** each output names its colour (and width) input, as today, so every
  output's colour and width can be changed in the Style tab.
  - Shapes: `arrow_up`, `arrow_down`, `circle`, `square`.
  - Positions: `above`, `below`, `on` (on the bar's close).
- **Panes:** `OVERLAY = True` draws on the price pane, `False` in a pane of its own (one
  pane per instance, as today).
  - Markers and backgrounds on an own-pane indicator draw in that pane.
- **Per-bar colours** (for example a MACD histogram green above zero and red below) are
  left out of the first version. They are a small addition later, through a second
  boolean output.

### 2.4 The helper library: `ta`

`from openchart.indicators import ta`. Every helper takes and returns pandas Series aligned
to the bars, so they compose (`ta.ema(ta.rsi(close, 14), 9)`).

- **The definitions follow TradingView's documented ones**, including how each one starts:
  - `ema` and `rma` are seeded with the SMA of their first `length` values, as today;
  - `rsi` uses `rma` of gains and losses, and its first value is at bar `length`;
  - `atr` uses `rma` of the true range, and the first bar's true range is high − low;
  - `stdev` is the population deviation, TradingView's default;
  - `highest` and `lowest` include the current bar.
- **They are written from the public formulas** (Wilder's book and the Pine reference's
  definitions). No TradingView script source is copied.

The first set:

| Group | Helpers |
|---|---|
| averages | `sma`, `ema`, `rma`, `wma`, `vwma`, `hma` |
| oscillators | `rsi`, `stoch`, `cci`, `macd` → (macd, signal, hist), `mom`, `roc` |
| volatility | `tr`, `atr`, `stdev`, `bb` → (basis, upper, lower) |
| windows | `highest`, `lowest`, `highestbars`, `lowestbars`, `sum`, `cum` |
| series | `shift(x, n)` (Pine's `x[n]`, `n >= 1` only), `change(x, n=1)`, `nz(x, 0)`, `crossover`, `crossunder`, `cross`, `barssince(cond)`, `valuewhen(cond, x, n=0)`, `rising(x, n)`, `falling(x, n)` |
| pivots | `pivothigh(x, left, right)` and `pivotlow`: a pivot is reported on the bar where it is *confirmed*, `right` bars late, so there is no lookahead (the docs explain why markers appear late) |

Plain numpy, pandas and Python loops over the arrays also work. A loop over 5,000 bars
takes a few milliseconds.

### 2.5 Warmup and lookahead

- **`warmup(params)`** is unchanged: the server adds that many earlier bars, so values do
  not depend on where the chart's history starts. The helpers document what they need
  (`ema`: about 10 × length for 1e-8 agreement; `sma`: length).
- **When you save, the worker checks the script on the bars it is run on:**
  - **lookahead:** it runs once on all the bars and once with the last 50 removed. If any
    shared value differs, the script reads future bars (for example `shift(-1)`, a
    centred window or `bfill`). The editor shows a warning naming the output.
  - **warmup too short:** it runs once more starting `warmup` bars later. If the values
    still differ after the warmup, it warns "declared warmup is too short".
  - Neither check blocks the save. They warn. Lookahead in arbitrary Python cannot be
    *prevented*, only detected.
- **The built-in helpers cannot look ahead:** `shift` refuses negative offsets, and
  `pivothigh` reports late. Tests prove this for every helper (section 8).

## 3. Execution safety: the worker

### 3.1 Shape

```
 server process (tokens, alerts, feeds)            worker process (no secrets)
 ┌───────────────────────────────┐   pipes    ┌─────────────────────────────────┐
 │ IndicatorRunner               │──────────▶ │ openchart indicator-worker      │
 │  - queue, one job at a time   │ framed msgs│  - compiles source text it is   │
 │  - deadline per job (asyncio) │ ◀──────────│    sent, runs compute on arrays │
 │  - kill + respawn on timeout, │            │  - no data dir, no db, no env   │
 │    crash or memory limit      │            │    secrets, no path to anything │
 │  - cache of results           │            └─────────────────────────────────┘
 └───────────────────────────────┘              inside OS limits (3.2 / 3.3)
```

- **One long-lived worker** runs `openchart indicator-worker` (the same exe in the frozen
  build). It starts in the background when the server starts and is ready in about 1–2 s,
  most of which is importing pandas.
  - Starting a process for every computation would cost that 1–2 s each time, which is too
    slow for once-a-second updates.
  - Up to 2 workers can be configured. The default is 1.
- **The server sends it source text and arrays**, never a path:
  - the script source (cached in the worker by its hash);
  - the bars as raw float64 arrays;
  - the parameters and `info`.
- **It answers with:**
  - the outputs, as arrays;
  - or an error with the traceback, cut down to the script's own frames, and its line
    number;
  - or, for "describe", the declared schema.
  - **The server never imports custom code.** It does not even read the schema itself: it
    asks the worker.
- **Clean start:**
  - an environment built from nothing: `SYSTEMROOT` and a `PATH` to the runtime only (no
    `OPENCHART_*`, no tokens, no `TEMP` inside the data folder);
  - its own empty working folder;
  - Python in isolated mode;
  - no command-line arguments carrying paths or secrets.
- **Audit hook, as a courtesy and not a wall:** after its imports, the worker installs a
  Python audit hook. It refuses `open`, `socket`, `subprocess` and `ctypes` loads with a
  readable error ("scripts cannot open files"), so honest mistakes fail clearly. Python
  documents that audit hooks are not a sandbox. The OS limits below are the actual
  protection.
- **Timeout:** each job has a deadline, 5 s by default and configurable from 1 to 30 s.
  When it passes, the server kills the worker (not the script: there is nothing smaller to
  kill), answers that request with "timed out after 5 s", and starts a new worker.
  - Other requests waiting in the queue are retried once on the new worker.
- **Quarantine:** a definition that crashes, times out or hits the memory limit 3 times in
  a row is marked "stopped after repeated failures". It is not run again until its file
  changes. This keeps one bad script from killing the worker for everyone.
- **Isolation from the server's work:**
  - the worker runs at below-normal priority (created at normal so that it can start on a busy machine, lowered by the server after its hello and read back: `docs/investigations/worker-start-under-load.md`);
  - the server talks to it with asyncio pipes and never blocks its event loop;
  - the feeds, candle building, alert engine and watchdog never wait on it.
  - Indicator alerts remain deferred. When they come, they will treat a failed or late
    computation as "no value", never as a crash.

### 3.2 Windows

There are two layers. The job object always applies. The identity layer is AppContainer,
with a fallback if AppContainer cannot be used.

**Job object** (created by the server, worker assigned at creation):

| Limit | Setting | Effect |
|---|---|---|
| `JOB_OBJECT_LIMIT_PROCESS_MEMORY` | 512 MB (configurable) | allocations beyond fail; Python raises `MemoryError` or the process ends |
| `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` = 1 | | the worker cannot start any other program |
| `JOB_OBJECT_CPU_RATE_CONTROL` hard cap | 25% of the machine (configurable) | a busy loop cannot starve the feed |
| `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | | if the server dies, the worker dies with it |
| `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` | | no error dialog left hanging under the service |
| UI restrictions | no clipboard, no desktop switching, no global atoms | |
| wall-clock deadline | enforced by the server (job objects have CPU time limits, not wall time) | hangs that sleep are caught too |

**Identity, preferred: AppContainer.** The worker starts in an AppContainer
("OpenChart.IndicatorWorker") with **no capabilities**.

- **What it enforces:**
  - **no network at all**, loopback included (no `internetClient` or
    `privateNetworkClientServer` capability), so it cannot reach OANDA, Telegram or the
    server's own API;
  - **no file access except where its SID is granted.** Your data folder, `secret.key`,
    your user profile and the source checkout all stay closed. The install step grants it
    read and execute on the runtime folder only.
  - **no access to your Credential Manager entries:** an AppContainer gets its own
    credential partition. This gets a test.
  - **no writing to other processes**, including the server.
- **What it does not enforce:**
  - it can still read files that Windows makes readable to every app, such as
    `C:\Windows` and much of `Program Files` (nothing of ours);
  - it can still use its CPU and memory budget;
  - it can write into its own small AppContainer folder. We remove write access to it at
    setup if the spike shows the worker runs without it.
  - Windows kernel bugs are out of scope.
- **The setup step** (in `install-service.ps1`, and `openchart indicator-sandbox setup` for
  source installs) creates the profile and grants the runtime folder.
  - For a source install, that means your venv and the Python it was made from. **This
    changes ACLs on your PC, so it runs only when you choose to run it.**

**Fallback: a restricted token**, used when AppContainer creation fails (to be found out in
the spike):

- a token with Administrators and SYSTEM set to deny-only, all privileges removed, and Low
  integrity;
- the key file (SYSTEM: read, Administrators: full) and the service data folder (SYSTEM,
  Administrators, you) then become unreadable;
- **it does not block the network.** Blocking the network needs a separate
  `openchart-worker.exe` with an outbound-block firewall rule. In that mode, editing in the
  browser stays off unless you turn it on yourself (section 3.6).

The **spike (milestone CI-0)** settles which of these works under LocalSystem with the
PyInstaller folder and with your venv, before anything else is built.

### 3.3 Docker

**Preferred: a sidecar container.** `docker-compose.yml` gains a second service built from
the same image:

```yaml
  indicator-worker:
    image: openchart:local
    command: ["openchart", "indicator-worker", "--listen", "/run/ocw/sock"]
    user: "10002"              # not the server's 10001
    network_mode: none
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 512m
    memswap_limit: 512m
    cpus: 1.0
    pids_limit: 32
    volumes: [ "worker-socket:/run/ocw" ]   # NOT the data volume
    restart: unless-stopped
```

The server reaches it through a Unix socket on a small shared volume. Inside the sidecar,
each computation runs in a forked child (fork is cheap on Linux once pandas is imported)
with `RLIMIT_AS`, `RLIMIT_CPU` and an alarm. A hang or memory blow-up kills only that
child, and the container's cgroup limits are the outer wall.

- **What it enforces:**
  - no network (only a loopback interface of its own);
  - the data volume, the database and `secrets.enc` are not mounted;
  - `OPENCHART_SECRET_KEY` is not in its environment;
  - the server's processes are invisible (separate PID namespace);
  - hard caps on memory, CPU and process count;
  - a read-only root filesystem.
- **What it does not enforce:**
  - the kernel is shared with the host: a container is not a virtual machine;
  - the worker can read the image's own files (which are public anyway).
- **Plain `docker run`** (one container, no compose) can only start the worker as a child
  with rlimits and the same user.
  - That child could read `/data` and `/proc/<server>/environ`, which holds the secret key.
  - So in that mode the server reports isolation as **"process only"**, browser editing is
    forced off, and the docs say to use compose.

### 3.4 What a failing script does to the rest

| Script does | Result |
|---|---|
| raises | its legend row shows "⚠ SMA cross: ZeroDivisionError (line 23)"; its lines are cleared; other indicators, the chart, alerts and the feed carry on |
| returns wrong lengths or types | same, with the reason ("output 'fast' has 4,990 values for 5,000 bars") |
| loops forever | killed at the deadline; "timed out after 5 s"; the worker restarts in 1–2 s; other indicators on screen update again after that |
| allocates 10 GB | the job or cgroup limit stops it; "ran out of memory (512 MB limit)"; same restart |
| fails 3 times in a row | quarantined until its file changes |
| tries to read env vars, files, the network, start programs | no secrets in the environment; file, network and process attempts fail (section 8 lists what each probe sees) |

### 3.5 Built-in indicators

Built-in indicators keep running inside the server: they are code we ship and test. This
also keeps EMA working if the worker cannot start (for example after a failed sandbox
setup). Decision **D5**.

### 3.6 The "allow editing custom indicators in the browser" setting

- **Values:** `auto` (the default), `on`, `off`.
- **`auto` is on** when the server binds only to localhost (`127.0.0.1`, `::1`,
  `localhost`), and **off** for any other address, Tailscale's included.
  - Docker always binds `0.0.0.0` inside the container, so `auto` means off there.
- **Where it is set:** `OPENCHART_INDICATOR_EDITING` or `openchart settings set
  indicator_editing on`, not from the browser. Settings shows it read-only, with the
  reason (decision **D2**).
  - If the browser could turn it on, anyone who got hold of your session (a stolen cookie,
    an unlocked phone) could turn it on and then run code on your PC.
- **When it is off:**
  - custom indicators still show in the menu and run on charts;
  - Import, New, Edit, Save, Run, Rename, Duplicate, Delete and source export answer `403`;
  - the menu says how to change it.
- **Files placed in the folder keep working either way**, and the docs explain why:
  putting a file there already takes access to the server's disk, which is more than the
  setting protects. They still run in the worker.
- **Weak isolation:** if the worker reports weaker isolation than intended ("restricted
  token" without a firewall rule, or "process only"), `auto` behaves as off, and the Custom
  section shows a one-line notice.

### 3.7 The honest security note (it goes into the docs)

The worker does **not** make custom code safe to accept from strangers.

- **What it protects against** is your own mistakes and run-of-the-mill malicious code you
  pasted without reading:
  - scripts that hang, eat memory or crash;
  - attempts to read your tokens and database, call home, or start programs.
- **What it does not protect against:**
  - a determined attacker with an OS or kernel exploit;
  - a script that returns plausible but false values;
  - scripts interfering with each other inside the shared worker (they all belong to you).
- **Treat a custom indicator like a program you run.** That is why browser editing is off
  for non-local installs unless you turn it on.

### 3.8 Performance

- **What is computed:** what the chart holds (pages of up to 5,000 bars, as today) plus
  the warmup. Older history, when you scroll back, is computed one page at a time.
- **Cache in the server:**
  - **the key:** the instance's definition id, the code hash, the instrument, timeframe,
    price type, input values and the page's bar range;
  - **the size:** at most about 100 MB, least recently used first;
  - **when it is cleared:** when the code changes, and when those bars are rewritten (a
    repair or refill already announces this).
  - **Style-only changes** (colour or width) never recompute.
- **The forming bar:** only `warmup + 2` bars are sent, once a second at most, and one
  computation is shared by every open tab showing that chart.
  - When the warmup is honest, the tail matches a full computation to about 1e-8 (the
    save-time check in 2.5 warns otherwise).
- **Target for a typical script** (3–6 helper calls, such as SMA cross, RSI with bands or
  a Bollinger fill) on 5,000 bars plus warmup:
  - **compute:** at most 20 ms in the worker;
  - **request:** at most 60 ms end to end at the 95th percentile with the worker warm;
  - **forming bar:** a tail update at most 15 ms;
  - **worker start:** at most 2 s, in the background at server start;
  - **worker memory:** about 80–120 MB idle.
  - These get measured on your PC in CI-1 and written into `docs/decisions.md`.

## 4. API

The existing endpoints stay. New ones answer `403` when browser editing is off; the reason
is given in section 3.6.

| Method and path | Purpose |
|---|---|
| `GET /api/indicators` | catalog in two sections, `builtin` and `custom` (with status: ok / error / quarantined), load errors, `editing` (on/off and why), `isolation` |
| `GET /api/indicators/{kind}/{id}` | values, as today (`instrument, timeframe, price, params, from, to, limit`); custom ones go through the worker |
| `GET /api/custom-indicators/{id}/source` | the code (editor; export) |
| `POST /api/custom-indicators` | New from a template, or Import (`{id?, source}`); validates through the worker, saves even with errors (so work isn't lost), returns the problems |
| `PUT /api/custom-indicators/{id}/source` | Save: stores a version, then reloads; returns schema or errors, plus the lookahead/warmup warnings |
| `PATCH /api/custom-indicators/{id}` | Rename (`{title}`) |
| `POST /api/custom-indicators/{id}/duplicate` | Duplicate |
| `GET /api/custom-indicators/{id}/usage` | charts and layouts using it |
| `DELETE /api/custom-indicators/{id}` | `409` with the usage if in use; `?confirm=true` removes the instances too |
| `GET /api/custom-indicators/{id}/versions`, `POST …/versions/{v}/restore` | history |
| `POST /api/custom-indicators/preview` | Run: unsaved source + chart query → values or an error with traceback and line (nothing stored) |
| `GET/PUT /api/charts/{source}/{instrument}/indicators` | the chart's instances (D1) |

- **Broadcasts:** WebSocket messages `indicators_changed` (catalog) and
  `chart_indicators_changed` (one chart) keep other tabs and the phone in step.
- **Export** is a plain download of the `.py`.
- **Import** takes a `.py` up to 256 KB and suggests the id from the file name.

## 5. UI

### 5.1 Indicators menu

```
┌ Indicators ──────────────────────────── ✕ ┐
│ 🔍 search                                  │
│ BUILT-IN                                   │
│   Moving Average Exponential               │
│ CUSTOM                     [New] [Import]  │
│   SMA cross                     ✎ ⋯       │
│   RSI with bands                ✎ ⋯       │
│   ⚠ Broken idea  (error, line 12)  ✎ ⋯     │
│   ⛔ Busy loop   (stopped: timed out)  ✎ ⋯ │
│ Files that did not load: …                 │
│ Folder: C:\ProgramData\OpenChart\data\ind… │
└────────────────────────────────────────────┘
```

- Clicking a row adds the indicator to the current chart.
- ✎ opens the editor.
- ⋯ offers Duplicate, Rename, Export, Delete.
- When editing is off, ✎, New and Import are hidden, and a line explains why.

### 5.2 Editor

The editor is a panel docked on the right, so the chart stays visible for the live
preview. On a phone it fills the screen.

```
┌ SMA cross  (sma_cross.py) ─ Versions ▾ ─ [Run] [Save] ✕ ┐
│  1 │"""SMA cross …"""                                     │
│  2 │from openchart.indicators import …                    │
│ …  │                                                      │
│ 23 │    up = ta.crossover(fast, slow) / 0   ◀ red marker  │
├─────────────────────────────────────────────────────────── ┤
│ ⚠ ZeroDivisionError: division by zero                     │
│   line 23, in compute                                     │
│ ⚠ Lookahead: output "up" changes when later bars arrive   │
└────────────────────────────────────────────────────────────┘
```

- **Run** draws a preview of the unsaved code on the current chart, as a dashed "preview"
  instance. Nothing is stored. Closing the editor removes the preview.
- **Save** stores the code and updates every chart using it.
- **Ctrl+S** saves and **Ctrl+Enter** runs.
- **Errors** link to their line.
- **The library:** CodeMirror 6 (MIT) with its Python mode only, loaded only when the
  editor first opens. I estimate about 150 KB compressed and will measure it in CI-4. Its
  licence goes into the third-party list.

### 5.3 On the chart

- **Legend rows** are as today, with an error state.
- **The settings dialog** is generated as today, with the new `String` input.
- **Added drawing code:** histogram and area series (part of lightweight-charts); markers
  (its `createSeriesMarkers`); and two small drawing plugins, one for fills between two
  lines and one for background tints. Levels are price lines on the instance's first
  series.

### 5.4 Deleting a definition that is in use

A confirmation lists where it is used:

> "SMA cross" is on 2 charts (XAU_USD, EUR_USD) and in 1 saved layout ("Gold H1"). Deleting
> it removes it from all of them.

After you confirm, the file and its history move to `.deleted/`. Its instances are removed
from the charts and layouts, and each open page gets the broadcast.

## 6. Risks

1. **AppContainer under LocalSystem and PyInstaller** (DLL loading, profile creation,
   pandas import) is unproven. That is why CI-0 is a spike first, with the restricted-token
   fallback ready.
2. **Running the spike as LocalSystem needs a temporary service** (`OpenChartTest`, as for
   storage step 1). I will ask before creating it.
3. **The source-install setup changes ACLs** on your venv and base Python (grant read for
   the worker's SID). It is reversible: `openchart indicator-sandbox remove`. It runs only
   when you choose to run it.
4. **CPU contention on your PC:** below-normal priority and the 25% cap. The live feed
   gets measured with a busy-loop script running.
5. **Behaviour change (D1):** indicators stop following you across instruments.
6. **Reference values:** subtle seeding differences (RSI's first value, ATR's first true
   range, stdev bias) are what make values "almost" match TradingView.
   - Tests use reference implementations written independently from the formulas, plus
     textbook examples.
   - The strongest check is optional and yours: export a TradingView chart with RSI, ATR
     and BB on `OANDA:XAUUSD` H1, and I compare it with our values on the same bars.
7. **Lookahead and short warmups** in user code are detected, not prevented (2.5).
8. **The Docker sidecar needs compose.** A plain `docker run` gets "process only" and
   editing off.
9. **Folder files move out of the server process.** Anything a file of yours did that the
   worker forbids (reading a file, for example) would now fail. I don't expect any; your
   folder can be checked first.
10. **Disk:** history is capped at 10 versions per definition, and `.deleted/` is never
    emptied automatically. The Custom section could show its size.

## 7. Milestones and sizes

| # | Content | Size |
|---|---|---|
| **CI-0** | Spike: start a Python worker in an AppContainer and a job object from Python (ctypes), as you and as LocalSystem (temporary service, after asking), with the PyInstaller folder and the venv; import pandas; probe file, network, Credential Manager, child process; measure the start time. Same probes in the Docker sidecar (CI only: no Docker on your PC). Report and go/no-go on 3.2. | 1–1.5 days |
| **CI-1** | Worker + runner: `indicator-worker` command, protocol, job limits, the identity layer from CI-0, timeout/memory kill, respawn, quarantine, audit hook, compose sidecar, folder files moved into the worker, the editing setting (read-only in the UI), security docs, performance measurement on your PC | 3–4 days |
| **CI-2** | Authoring: `ta` helpers + reference tests, `String`, `info`, long column names, new output kinds (schema + validation + drawing in the chart), the lookahead/warmup checks, `docs/custom-indicators.md` with the four examples and the Pine cheat sheet | 3–4 days |
| **CI-3** | Per-chart instances on the server (D1): table, endpoints, layouts carry them, migration from `localStorage`, the menu's sections and search, where-used, delete-in-use | 2 days |
| **CI-4** | Editor and management: CodeMirror, New/Edit/Duplicate/Rename/Delete/Import/Export, Save/Run, error panel with line links, live preview, versions | 3 days |
| **CI-5** | Cache and tail updates, shared across tabs; final measurements against the 3.8 target | 1–1.5 days |

That is about 13–16 working days in all. Each milestone gets its own acceptance check and
stop, as usual. CI-0 is the only one that may change the rest of the plan.

## 8. Tests planned

- **Input schema validation:** every rule in 2.1, each with a failing file and its
  message.
- **No lookahead:**
  - for every helper and the four doc examples: computing on `bars[:k]` gives the same
    value at `k-1` as computing on all the bars, for many `k`;
  - changing bars after `k` leaves outputs up to `k` unchanged;
  - the save-time check flags a `shift(-1)` script and passes the examples.
- **Warmup:** values after the warmup are the same whichever bar history starts at.
- **Helpers against reference values:**
  - independent loop implementations of the formulas on fixed data;
  - Wilder's published RSI and ATR example tables;
  - EMA and RMA seeding;
  - the optional TradingView export comparison.
- **Worker timeout:** `while True: pass` and `time.sleep(60)` are both killed at the
  deadline. The server keeps answering `/api/health` and candles during the hang, and the
  next request works on the new worker.
- **Memory:** allocating 2 GB stops at the limit with "ran out of memory". The server's
  memory is unchanged.
- **Probes** (each test records what the script sees, and the docs list it):
  - read `os.environ` for `OPENCHART_*` and `OANDA*`: absent;
  - open the database, `secrets.enc` and `secret.key` by absolute path: denied;
  - list the data folder and the user profile: denied;
  - read Credential Manager `OpenChart/<id>`: denied;
  - connect to `127.0.0.1:<server port>` and to the internet: denied;
  - `subprocess.run("cmd")`: denied;
  - `ctypes`: refused by the audit hook (and denied by the OS anyway).
- **Quarantine** after 3 failures, and its release after the file changes.
- **The feed and alerts keep running** while a script hangs (fake provider: ticks keep
  arriving and an alert still fires).
- **A script written on gold used on EURUSD:** same file, different decimals (`info`) and
  hours; the values are correct and the instances are independent.
- **Layouts keep instance values:** save, change, reload, and the values come back;
  definition edits fall back per input (1.2).
- **Delete-in-use:** `409` with the usage, then `confirm` removes the instances
  everywhere; the file goes to `.deleted/`.
- **Editing setting:** `auto` with the localhost and other binds; every write endpoint
  answers `403` when it is off; folder files still run.
- **Versions:** 11 saves keep 10; restore works.
- **Compatibility:** today's `ema.py` and an M7-format custom file load unchanged.

## 9. Decisions for you

- **D1. Instances per chart on the server** (recommended), versus keeping today's list,
  which belongs to the browser and follows instrument switches. Your brief reads like D1,
  but it changes current behaviour, so I'm asking.
- **D2. The editing switch only on the server side** (environment variable or CLI;
  recommended), versus a toggle in Settings protected by your password.
- **D3. Docker uses the sidecar** (recommended). Without compose, editing is forced off.
- **D4. Windows uses AppContainer first, with the restricted token as fallback**, settled
  by the CI-0 spike. The spike needs a temporary LocalSystem test service, which I will
  ask for at that point.
- **D5. Built-in indicators stay in the server process**; only custom code goes to the
  worker.
- **D6. Delete moves files to `.deleted/`** (recoverable by hand), instead of deleting
  them for good.
