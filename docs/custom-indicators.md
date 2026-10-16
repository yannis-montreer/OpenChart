# Custom indicators: where your code runs

Your own indicator files (`<data dir>/indicators/*.py`, written as described in
[indicators.md](indicators.md)) never run inside the server. They run in the **indicator
worker**, a separate process started with less than the server can do. This page says what
that protects and what it does not. The design is in `custom-indicators-design.md`; the
spike that proved it, in `ci0-spike-report.md`.

Status: CI-1 (the worker, the isolation, the editing setting). The `ta` helpers and new
output kinds come in CI-2, per-chart instances and the menu's sections in CI-3, the editor
in CI-4.

## What the worker is

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
| Docker with `docker-compose.yml` | the `indicator-worker` container | met | per the setting (`auto` is off in Docker) |
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

## Checks for lookahead and a short warmup (CI-2)

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

## Editing in the browser (from CI-4)

Editing custom code in a browser needs **all** of:

- the minimum isolation (above); no setting overrides it;
- the setting, which only the server's own machine can change:

  ```
  openchart settings set indicator-editing auto   # the default
  openchart settings set indicator-editing on
  openchart settings set indicator-editing off
  openchart settings set behind-proxy on          # a reverse proxy is in front
  ```

  (`--data-dir C:\ProgramData\OpenChart\data` for the service.) It works while the server
  runs and takes effect on the next request; no web page or API can change it.
- with `auto`: a request from the server's own machine. Its TCP peer is 127.0.0.1 or ::1,
  it carries none of `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `Tailscale-User-Login`,
  and `behind-proxy` is off. On the Windows service, which listens on 0.0.0.0 for
  Tailscale, that means yes in the browser on the PC (http://127.0.0.1:8000, the viewer)
  and no from the phone. In Docker requests come from the bridge, so `auto` is off.
- the login and the same-origin check, as for every change.

A proxy that adds none of those headers cannot be detected: with a proxy in front, set
`behind-proxy on` or `indicator-editing off`.

## What it does not protect against

- **A determined attacker** with an operating-system or kernel exploit. A container is not
  a virtual machine, and an AppContainer is not either.
- **A script that returns plausible but false values.**
- **Scripts interfering with each other** inside the shared worker (they are all yours).
- **On an install below the minimum, almost anything a program running as you can do.**

Treat a custom indicator like a program you run. That is why browser editing is off unless
the install isolates code and you allow it from the server's machine.
