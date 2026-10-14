# CI-0 spike: running custom indicators in an isolated worker

Status: **interim**. The Windows runs as the owner's user are done. Still to run:

- the **LocalSystem run**, as a temporary service, at a moment the owner chooses;
- the **Docker run**, in CI, which needs the branch pushed.

The final go/no-go comes after both. Code: `tools/ci0_spike/`.

## Minimum acceptable isolation

A worker reaches the minimum when **all** of the following hold, each proven by a probe:

1. **No secrets in its environment:** no `OPENCHART_*` (the secret key), no tokens.
2. **It cannot read the server's secrets or data:**
   - the data folder, the database and `secrets.enc`;
   - the key file;
   - Credential Manager entries, the registry, the user profile, and a `.env`.
3. **No network:** not the server's own port on 127.0.0.1, not the LAN, not the internet.
4. **It cannot touch the server process:** no opening it, no reading its memory.
5. **It cannot start programs.**
6. **Its limits hold:** a memory cap, a CPU cap and a deadline. It dies when the server
   dies. A hang or a memory blow-up never reaches the server.

**Browser editing stays off below the minimum, with no override.** The `on` setting cannot
turn it on there. Files placed in the indicators folder still run below the minimum, in the
weaker worker. The Custom section then shows "weaker isolation: this install cannot
isolate custom code from your data and tokens".

| Install | Isolation | Meets the minimum |
|---|---|---|
| Windows service (installer sets up an AppContainer) | AppContainer + job object | **yes** (as the user; LocalSystem run pending) |
| Source install (no ACL changes, per the owner's decision) | restricted token + job object | **no**: reads files, Credential Manager and the network (below) |
| Docker with compose (sidecar) | separate container, no network, no data volume | to be proven in CI |
| Docker without compose | child process, same user | no (by design; editing off) |

## What was run

The **driver**, `run_spike.py`, plays the server. It starts the worker in four ways
(`winsandbox.py`, ctypes):

- **plain:** for comparison;
- **restricted:** the venv of a source install;
- **AppContainer + a Python copy;**
- **AppContainer + the PyInstaller build:** what the service would ship.

Every worker runs in the same job object:

- a 512 MB memory limit;
- one process only;
- a hard cap of 50% of one core;
- killed when the job closes;
- no error dialogs and no UI access.

**The probes:**

- **Targets:** decoys made for the run and removed after it: a `.env`, a database, a key
  file, a Credential Manager entry and a registry value.
- **Reporting:** a probe reports allowed or denied, never content.
- **Never touched:** the real `.env`, data folder and tokens.

**ACLs** changed only on the two runtime copies in the scratch folder. The owner's venv and
base Python were untouched; the restricted mode only reads them.

## Results as the owner's user (Windows 10 19045, not elevated)

| Probe | plain | restricted (venv) | AppContainer (Python copy) | AppContainer (PyInstaller) |
|---|---|---|---|---|
| identity | Medium | Low | Low, AppContainer | Low, AppContainer |
| environment | **secrets visible** | clean | clean | clean |
| read `.env` in a user folder | ALLOWED | ALLOWED | denied | denied |
| read the database | ALLOWED | ALLOWED | denied | denied |
| read the key file | ALLOWED | ALLOWED | denied | denied |
| list the data folder | ALLOWED | ALLOWED | denied | denied |
| list the user profile | ALLOWED | ALLOWED | denied | denied |
| write into the data folder | ALLOWED | denied | denied | denied |
| write into its working folder | ALLOWED | denied | denied | denied |
| write into its own TEMP | ALLOWED | (none set) | **ALLOWED** (its container folder) | **ALLOWED** |
| write into `Windows\Temp` | ALLOWED | denied | denied | denied |
| Credential Manager entry | ALLOWED | **ALLOWED** | denied | denied |
| registry value (HKCU) | ALLOWED | **ALLOWED** | denied | denied |
| open the parent process | ALLOWED | denied | denied | denied |
| connect to a listening port on 127.0.0.1 | ALLOWED | **ALLOWED** | blocked (no connection) | blocked (no connection) |
| connect out to 192.0.2.1:9 | left the PC | **left the PC** | denied at once | denied at once |
| start a program | refused (job) | refused (job) | refused (job) | refused (job) |

Notes on the network rows:

- The outbound probe aims at 192.0.2.1, a reserved documentation address: one TCP
  connection attempt, no data. "Left the PC" means the attempt was not refused locally.
- No API or internet host was contacted.

The limits held in every mode:

| Measure | Result |
|---|---|
| typical script on 5,200 bars (EMA, SMA, RSI, Bollinger band, a cross) | 2.1–2.4 ms in the worker; **2.9–3.0 ms round trip** (median), at most 6.6 ms |
| worker start, including the numpy and pandas import | 1.1 s (Python copy) to 2.4 s (PyInstaller) |
| deadline: busy loop and `sleep(3600)` | killed 10–19 ms after the 1 s deadline; a new worker answered 1.0–1.9 s later |
| memory: allocate 2 GB under a 512 MB limit | `MemoryError` at 384–448 MB; the worker kept answering |
| CPU cap 50% of one core, 4 s of busy loop | 51–53% measured |
| parent killed without cleanup | worker gone within 1 s in every mode |

The design's targets were at most 20 ms to compute and 60 ms end to end on 5,000 bars, and
at most 2 s to start the worker. Every run met them except the PyInstaller start in AppContainer
(2.4 s), which is borderline. It happens once, in the background, and again only after a
kill.

## Findings that change the plan for CI-1

1. **AppContainer needs `LOCALAPPDATA` in the environment.**
   - Without it, CreateProcess fails with error 203. Windows maps the variable, `TEMP` and
     `TMP` to the container's own folder.
   - The worker then sees 8 variables, none of them secret.
2. **The container can write into its own folder.**
   - That is the one write AppContainer allows. A script could fill the disk there.
   - CI-1 options: remove that write access when the installer creates the profile (if the
     worker runs without it), or cap and clear the folder at each worker start.
3. **A venv's `python.exe` cannot be the worker.**
   - On Windows it is a launcher that starts the base Python as a child, which the job's
     one-process limit refuses (exit 101).
   - Source installs must start the base interpreter and add the venv's packages, as the
     spike does with `--site`.
4. **Thread variables are needed in the clean environment.**
   - The plain mode inherited the full environment. There, numpy's math library started
     one thread per core and committed about 400 MB before any work, so the memory test hit
     the limit after 64 MB.
   - The clean environment sets `OPENBLAS_NUM_THREADS=1` (and the OMP/MKL equivalents),
     which avoids it. CI-1 keeps these.
5. **The restricted token below the minimum.**
   - It is useful against accidents: no writes anywhere, no access to the server process,
     and the job limits.
   - It does not stop reading files, Credential Manager or the network. On a source install
     a malicious script could read the OANDA token from Credential Manager.
   - Today's folder files can already do that, and more, inside the server. So moving them
     to the restricted worker is still an improvement, but it is not isolation, and the
     notice says so.
6. **Kill and restart are fast enough.** A killed worker is replaced in about 1–2 s, so
   the design's "quarantine after 3 failures" keeps a looping script from costing more
   than a few seconds per edit.

## Still to run

### LocalSystem (needs your go and your chosen moment)

`tools/ci0_spike/service-spike.ps1` runs elevated, with one UAC prompt, and does the
following:

1. **Builds a service-like layout** in `C:\ProgramData\OpenChartTest`:
   - a copy of the PyInstaller worker;
   - a decoy data folder and key file with **the installer's exact ACLs**: data
     SYSTEM/Administrators/you, key file SYSTEM read and Administrators full.
2. **Installs a temporary NSSM service `OpenChartTest`** (LocalSystem, manual start, no
   restart on exit). It runs the driver once, in three modes:
   - plain as SYSTEM;
   - restricted (SYSTEM and Administrators deny-only);
   - AppContainer.
   - No port is opened beyond the driver's own 127.0.0.1 listener while it runs. There is
     no firewall rule and no broker.
3. **Copies the report out**, removes the service and deletes the folder.
4. **Proves the cleanup:**
   - `sc query OpenChartTest` is 1060;
   - the folder and key file are gone;
   - there are 0 firewall rules;
   - there are no spike processes and no listening ports;
   - SYSTEM's AppContainer profile, its folder and the registry decoy are gone.

The open questions it answers:

- Can LocalSystem create the AppContainer profile?
- Does the PyInstaller worker start in it from `ProgramData`?
- Does the restricted token, as SYSTEM, still start the worker? (It needs the release
  folder to be readable without SYSTEM.)

### Docker (CI only, needs the branch pushed)

- **The workflow:** `.github/workflows/ci0-spike.yml` runs
  `tools/ci0_spike/docker/compose.spike.yml`:
  - **a stand-in server** with a decoy secret key in its environment and a decoy database
    in `/data`;
  - **the sidecar worker** as the design proposes: uid 10002, no network, a read-only
    root, all capabilities dropped, `no-new-privileges`, 512 MB, 0.5 CPU, 32 processes,
    and only a socket volume shared with the server.
- **Inside the sidecar,** each request runs in a forked child with `RLIMIT_AS` and a
  deadline.
- **The job fails unless the minimum holds:**
  - a clean environment;
  - no `/data` or `secrets.enc`;
  - no server, no outbound connection, no writes to `/`;
  - a hang killed while the worker carries on.
- **It also reports** what a child sees in `/proc`, whether 64 processes can start, and
  what a 2 GB allocation does with and without `RLIMIT_AS`.
- It runs on a push of `custom-indicators`, or by hand. Pushing needs your OK.

## D2: when "auto" allows browser editing (a recommendation; not built)

The minimum isolation comes first. Below it, editing is off whatever this rule says.

**One command for your install, as planned:**

```
openchart settings set indicator-editing on --data-dir C:\ProgramData\OpenChart\data
```

- It writes the setting into the database; the server reads it on each request, so no
  restart is needed.
- It is run on the PC, from a PowerShell where `openchart.exe` is the service's.
- No HTTP endpoint can change it.
- The alternative, an environment variable through NSSM, would need a restart. It would
  also mean rewriting the service's `AppEnvironmentExtra`, which already holds the key file
  path, so it is easy to get wrong.

**The two rules compared:**

| | Bind address (`auto` = on only when bound to 127.0.0.1/::1) | Request origin (`auto` = on only for requests whose TCP peer is 127.0.0.1/::1) |
|---|---|---|
| Your service (binds 0.0.0.0 for Tailscale) | off everywhere, even in the browser on the PC | **on in the browser on the PC** (`http://127.0.0.1:8000`, and the viewer); off from the phone and other Tailscale devices |
| Docker | always off (the container binds 0.0.0.0) | off: published-port requests arrive from the Docker bridge, not loopback |
| Reverse proxy on the same PC (Caddy, nginx, `tailscale serve`) | **unsafe too**: the server then binds 127.0.0.1, so `auto` is on, and remote requests come in through the proxy | **unsafe as such**: the proxy connects from 127.0.0.1, so every remote request looks local |
| Other programs on the PC | n/a (decided once at start) | look local; they still need your login (a session cookie), like any request |
| Opening the Tailscale address on the PC itself | off | off (the peer is the Tailscale address); use 127.0.0.1 |

**The proxy caveat, and how to handle it:**

- Any request that carries `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or
  `Tailscale-User-Login` (the header that `tailscale serve` adds) counts as remote.
- A setting `behind-proxy` turns `auto` into off.
- A proxy that adds none of those headers cannot be detected. The docs say so: with a proxy
  in front, set `off` or `behind-proxy`.

**Recommendation: the request-origin rule** with the header check. It matches what you
want ("yes on this PC, no from the phone"), works for your 0.0.0.0 bind with no command, and
is off in Docker. It is an extra condition only:

- the login and the existing same-origin check still apply to every editing request;
- `on` and `off` stay available as overrides;
- `on` still cannot go below the minimum isolation.

## Provisional go/no-go

- **Windows service: GO**, if the LocalSystem run confirms the user results.
- **Source install: GO for the restricted worker as a weaker mode**, with browser editing
  off, as decided.
- **Docker: pending CI.**
