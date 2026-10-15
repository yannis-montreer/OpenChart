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
