# CI-1 plan: the isolated indicator worker

Status: **plan only**, approved in principle on 2026-10-15. Work starts when the owner says
the M8 tests and the data move are done. Inputs:

- `docs/custom-indicators-design.md` (approved, with the owner's changes);
- `docs/ci0-spike-report.md`: findings 1 to 10, decisions D1 to D7, and the D2
  request-origin rule.

CI-1 delivers:

- the worker and the runner;
- isolation on each kind of install;
- the editing setting;
- the installer, uninstaller and compose changes.

Your indicator files in the data folder move out of the server process into the worker.
**The following are not in CI-1:**

- the `ta` helpers and new output kinds (CI-2);
- per-chart instances (CI-3);
- the editor and the management API (CI-4);
- the cache and tail updates (CI-5).

## 1. What each install does

| Install | Worker | Minimum isolation | Browser editing (`auto`) | Folder files |
|---|---|---|---|---|
| Windows service, release zip | AppContainer + job object + private window station and desktop | met | per the request-origin rule | run, isolated |
| Windows service, AppContainer setup failed (D7 a) | none | — | off | **do not run**; the Custom section says why and to rerun the installer; built-in indicators work |
| Windows service from a source checkout (`-Exe ...\.venv\...`) | none: AppContainer would need ACLs on the venv (not allowed); the restricted token cannot start under SYSTEM (CI-0) | — | off | **do not run** (same message, plus "use the release build for custom indicators") |
| Source install, run by the user (`openchart run`) | restricted token (Low integrity) + job object, the base Python with the venv's packages | not met | always off | run, with the "weaker isolation" notice |
| Docker with compose | sidecar container | met | off under `auto` (requests come from the Docker bridge); `on` allowed | run, isolated |
| Docker without compose | child process, same user | not met | always off | run, with the notice |

Built-in indicators always run in the server, as decided in D5.

## 2. Files

### New: `src/openchart/indicators/worker/`

| File | Content |
|---|---|
| `protocol.py` | Frames: a 4-byte length, a JSON header, then raw float64 arrays (as in the spike). The size is capped, and garbage is rejected. |
| `child.py` | **Worker side.** Receives source text and caches it by hash, compiles it under a file name such as `sma_cross.py` so tracebacks show the right file and line, runs `describe` and `compute`, and cuts tracebacks down to the script's own frames. After its imports it installs the audit hook (a courtesy, not a wall). Nothing but frames goes to stdout. |
| `runner.py` | **`IndicatorRunner` in the server**, with asyncio pipes or the socket: one worker by default, a queue, a deadline per job (5 s by default, 1 to 30 s), and kill and respawn. Quarantine after 3 failures in a row, until the file changes. One retry for queued jobs after a respawn. It never blocks the event loop. |
| `windows.py` | **From `tools/ci0_spike/winsandbox.py`:** the job object (memory, CPU cap, one process, kill on close, no dialogs, UI limits), the AppContainer launch (`LOCALAPPDATA` in the environment, finding 1), the named private window station and desktop (finding 7), the restricted token for source installs, and the exit code of early deaths (finding 10). |
| `posix.py` | **The sidecar (Docker):** a Unix-socket server, a child forked per request with `RLIMIT_AS` set to the current size plus the budget (CI-0 Docker finding) and `RLIMIT_CPU`, and a deadline. Also the "process only" fallback. |
| `isolation.py` | **Decides the level at start:** `appcontainer`, `sidecar`, `restricted`, `process-only` or `unavailable`, with the reason. It does this by starting a worker once and probing what matters: no secrets in its environment, the key file and data folder closed, no network to 127.0.0.1. The level is logged and returned by `/api/indicators`. |

### Changed

| File | Change |
|---|---|
| `src/openchart/indicators/_registry.py` | Custom files are **no longer imported** into the server: the worker answers `describe`. Built-in and custom ids live in separate namespaces (`kind`). Files are reloaded when their modification time or size changes, so no restart is needed. |
| `src/openchart/api/routes_indicators.py` | Custom values go through the runner. The catalog gains its `builtin` and `custom` sections, a status per definition (ok / error / quarantined / not run, with the reason), `isolation` and `editing`. |
| `src/openchart/server.py` | Starts and stops the runner, in the background at start. Exposes the isolation status. |
| `src/openchart/main.py` | New commands: `indicator-worker` (hidden: the worker itself), `settings get/set` (only the keys meant for it: `indicator-editing`, `behind-proxy`), and `sandbox check` (an offline check, as SYSTEM through the service or as the user). `self-test` also checks the worker. |
| `src/openchart/settings.py` | The `indicator_editing` (`auto`/`on`/`off`) and `behind_proxy` keys. |
| `src/openchart/api/editing.py` (new) | **The D2 gate.** Editing needs all of: the isolation minimum is met; the setting is `on`, or `auto` with a loopback peer (127.0.0.0/8, ::1), no `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Tailscale-User-Login` header, and `behind_proxy` off; and the usual login and same-origin check. CI-1 only applies it to what exists (the editing status in the catalog); CI-4's write endpoints use it. |
| `src/openchart/config.py` | Worker limits from the environment: `OPENCHART_WORKER_MEMORY_MB` (512), `..._CPU_PERCENT` (50% of one core), `..._TIMEOUT_S` (5), `OPENCHART_WORKER_SOCKET` (Docker). |
| `packaging/pyinstaller/openchart-server.spec` | Hidden imports for the worker package. |
| `packaging/windows/install-service.ps1` | A new step, "Indicator sandbox" (section 3). |
| `packaging/windows/uninstall-service.ps1` | A cleanup pass as SYSTEM before removal (section 5). |
| `packaging/build-windows.ps1` | The smoke test runs `openchart sandbox check` as the user. |
| `docker-compose.yml`, `Dockerfile` | **The sidecar service:** uid 10002, `network_mode: none`, read-only, `cap_drop: ALL`, `no-new-privileges`, 512 MB, 1.0 CPU, 32 pids, and a tmpfs socket volume only. The worker container's environment is emptied of `OPENCHART_*`. |
| `.github/workflows/ci.yml` | The Docker job runs the real compose with the sidecar and its probes. A **new Windows job** runs the release installer on the runner (port 18xxx, `-NoFirewall`), waits for "indicator sandbox: ready (AppContainer)" in the service log, probes, uninstalls and checks the cleanup. This makes the LocalSystem proof part of CI. |
| `.github/workflows/ci0-spike.yml`, `tools/ci0_spike/` | Removed, now that their code lives in the package. The spike report stays in `docs/`. |
| `docs/custom-indicators.md` (start), `docs/decisions.md`, `SPEC.md` §11, `README.md` | The security note, the isolation table above, the editing rule, and the settings command. |

### Database

CI-1 needs no table. The settings live in the existing `settings` table. The
`chart_indicators` migration comes in CI-3, numbered at merge time (the next free number).

## 3. Installer changes (`install-service.ps1`)

A new step, **"Indicator sandbox"**, after "Data folder" and before "Windows service":

1. **Release check.** If `-Exe` is not a release build (no `_internal` next to it, or it
   lies under a `.venv`):
   - Note: "custom indicators need the release build; with a source checkout they do not
     run under the service";
   - skip the remaining steps;
   - the service installs as before.
2. **The SID.** Run `openchart.exe sandbox sid`. It derives the AppContainer SID from its
   name with `DeriveAppContainerSidFromAppContainerName`; the SID depends on the name only,
   so it is the same for every account.
3. **The grant.** `icacls <release folder>\openchart /grant *<SID>:(OI)(CI)RX /T`: read and
   execute on the release folder only. The data folder and the key file keep their ACLs;
   the container has no access to them.
4. **Recording.** The SID is saved in the service's parameters
   (`AppEnvironmentExtra OPENCHART_SANDBOX_SID=...`), so the uninstaller can remove exactly
   that grant.
5. **After the service starts,** the installer waits up to 60 s for one of these lines in
   `service.log`:
   - `indicator sandbox: ready (AppContainer)` → Ok;
   - `indicator sandbox: unavailable: <reason>` → a warning naming the reason, and "custom
     indicators will not run; everything else works; rerun this installer after fixing it".
     **The install does not fail.**

The AppContainer **profile** is created by the server itself, as SYSTEM, when it starts.
`CreateAppContainerProfile` is idempotent, and that is the path proven in CI-0 run 3.

At that point the server also:

- removes the container's write access to its own profile folder (finding 2), after
  checking that the worker still starts;
- otherwise empties that folder at each worker start and caps its size.

## 4. Upgrading an M8 service install

Your M8 install has the service, the data folder `C:\ProgramData\OpenChart\data`, the key
file, and the release folder you unzipped. To upgrade:

1. **Back up first.** The installer runs `openchart backup --data-dir ...` itself before
   touching anything, prints the zip's path, and stops if the backup fails. This is new,
   and also covers the later storage migration.
2. **Unzip the new release** next to the old one (keep the old folder) and run its
   `install-service.cmd`. It updates the service in place, as it does today (same name,
   data folder and key file), and adds the sandbox step above.
3. **The first start of the new version:**
   - migrations as usual (none for CI-1);
   - the AppContainer profile is created;
   - your indicator files in `data\indicators\` now run in the worker. Anything they did
     that the worker forbids (reading a file, the network) now fails with an error in the
     legend. Your live data folder is not to be touched, so before the upgrade the owner
     says whether `data\indicators\` holds files of their own; if so, a copy gets checked
     for such calls.
4. **Nothing changes in the browser** until CI-3 and CI-4. Built-in indicators,
   layouts, alerts and drawings are untouched.

## 5. Rollback

| What failed | What happens | How to go back |
|---|---|---|
| Granting the release folder (step 3) | Warning; the service installs; custom indicators do not run (D7 a) | Nothing to undo; rerun the installer after fixing the cause |
| The server cannot create the profile or start a worker (as SYSTEM) | `indicator sandbox: unavailable: <reason>` with the exit code; custom indicators do not run; everything else works | Rerun the installer, or roll back the version (below) |
| The new version misbehaves in general | — | Run the **old** release's `install-service.cmd`: it points the service back at the old exe. CI-1 adds no migration, so the old version reads the same database. If CI-3 has landed by then, its table is new and unused by older versions, so they still work; the backup from step 1 is the safety net. |
| Remove the sandbox completely | — | `uninstall-service.ps1` (below), or rerun the installer with a new `-NoSandbox` switch, which removes the grant and the profile |

**The uninstaller** gains one step before it removes the service:

- it runs the service once as `openchart.exe sandbox remove` (SYSTEM, through NSSM, as the
  spike's cleanup pass did), which deletes the AppContainer profile and its folder;
- it removes the grant: `icacls ... /remove *<SID>`;
- it prints a proof list:
  - no profile mapping under `HKU\S-1-5-18\...\AppContainer\Mappings`;
  - no package folder;
  - no grant left on the release folder.

## 6. Tests

**Everywhere** (Linux, Windows, macOS where CI has them):

- the frame protocol: round trip, size cap, garbage rejected;
- the runner with a fake worker:
  - a deadline kills and respawns;
  - queued jobs retry once;
  - 3 failures quarantine the definition, and editing the file releases it;
  - the event loop stays responsive while a job hangs;
- `describe` through the worker equals what the M7 loader reads for `ema.py` and an
  M7-format custom file. **Compatibility.**
- **The registry:**
  - custom files are never imported into the server (a sentinel file that would write a
    marker on import leaves none);
  - mtime and size reload;
  - separate namespaces: a custom `ema.py` no longer replaces the built-in;
- **the D2 gate:**
  - loopback peer → allowed;
  - a Tailscale peer → refused;
  - each forwarding header → refused;
  - `behind_proxy` → refused;
  - `on` → allowed from anywhere, but still refused below the minimum;
  - `off` → refused;
- `settings get/set` accepts only its keys;
- **the feed and alerts keep running while a script hangs** (fake provider: ticks keep
  arriving and an alert fires).

**Windows** (CI runner as the user, plus the CI service job as SYSTEM):

- **job limits:**
  - `MemoryError` at the limit, and the worker keeps answering;
  - a hang and a sleep are killed at the deadline;
  - a child process is refused;
  - the worker dies with its parent;
- **AppContainer probes** against decoys: every row from CI-0 is denied;
- **the restricted source-install worker:** writes, the server process and child programs
  are denied; reads are allowed and asserted as such, so the notice stays honest;
- the early-death exit code reaches the catalog;
- **in the CI service job:**
  - a private desktop is used;
  - `ready (AppContainer)` appears in the log;
  - the uninstaller's proof list is all zeros.

**Docker** (CI): the sidecar probes from CI-0, with the corrected checks, on the real
compose file.

## 7. Size

About **4–5 working days**, up from the design's 3–4. The additions are the private desktop
and the service CI job, the D2 gate and settings command, the installer and uninstaller
steps, and the move of the folder files.

## 8. Separate, waiting for the owner's go

- **Small branch A** (after the M8 tests): the stale-minute fix A+B and the `stats` >100%
  display bug.
- **Small branch B** (when the owner says):
  - **the dead server on a changed secret key:** log it, start without broker credentials,
    and show it in the web page;
  - **permissions:** `secrets.enc` and the backups at 600 in Docker;
  - **the log line:** "listening on 0.0.0.0 for other devices" is misleading in Docker.
- **Storage step 1:** parked on `storage-1-candle-format`.
