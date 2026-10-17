# Roadmap

What is merged, what is parked on a branch, and what comes next. Decisions behind each
item are in `docs/decisions.md`. Updated 2026-10-16.

## Merged into main

| Work | PR |
|---|---|
| M0 Scaffolding, M1 OANDA provider, storage, backfill | #1 |
| M2 Aggregation, live candles, API, websocket | #2 |
| M3 Web chart | #3, #4 |
| M4 Setup wizard, login, settings | #5 |
| M5 Drawings (horizontal line, trendline, ray, extended line) | #6 |
| M6 Alerts and Telegram | #7 |
| M7 Indicators (EMA), chart types, layouts | #8 |
| M9 Second instrument by metadata only; downloads newest first; FX rollover break | #9 |
| M8 Packaging: Windows service, PyInstaller build, desktop viewer, Docker | #10 |
| Fixes after M8: stale tick-built minutes replaced by the broker's (options A and B), exact `stats` coverage, a changed secret key stops the server, private `secrets.enc` and backups, Docker notes | #11 |
| OANDA errors that explain themselves, unreadable tokens refused locally, an OANDA mock that enforces the API's rules | #12 |
| Storage step 1: integer prices and series keys (migration 0007), 57% smaller, applied to the owner's data | #13 |
| Data-folder lock: commands that write refuse while a server runs; the installer stops the service first (tested in CI) | #14 |
| v0.1.0: version, release notes, draft release from the notes file | #15 |
| Handoff: CLAUDE.md, this roadmap, local notes kept out of git | #16 |
| Custom indicators CI-1: your indicator files run in an isolated worker (AppContainer for the service, compose sidecar, restricted token or a job-limited process below the minimum, with a notice), the D2 editing gate and `openchart settings`, `openchart sandbox`, installer and uninstaller sandbox steps, CI as LocalSystem. Design, CI-0 report and CI-1 plan in `docs/` | #17 |
| Custom indicators CI-2 step (a): the `ta` helper library, 34 helpers matching TradingView's definitions (averages, series, oscillators, volatility, windows, pivots), each with a no-lookahead test and loop references; pivot and `highestbars` ties and `rising`/`falling` checked in TradingView. Warmup per helper in `docs/indicators.md` | #19 |
| Tests on one fixed clock (`tests/clock.py`): removes the time-of-day failure that broke every Windows job of #18 (a run crossing 17:00 New York) and seven failures on a Saturday or a Sunday before the open; a regression test runs the time-sensitive tests at moments around the breaks and the weekly close and open | #20 |
| Custom indicators CI-2 step (b): the output kinds (`Histogram`, `Area`, `Fill`, `Level`, `Marker`, `Background`), the `String` input, `info`, long column names, and schema and result validation in the worker and again in the server | #21 |
| Custom indicators CI-2 step (c): the save-time lookahead and warmup checks (they warn, never block), and a tool that breaks one rule at a time to prove the tests catch it | #22 |
| The indicator worker starts at normal priority and drops to below normal after its hello (a worker starved by busy cores did not start) | #23 |

**v0.1.0 is released as a pre-release** (owner, 2026-10-15).

## Parked on branches (not merged)

- **`storage-review`** (local, not pushed): the storage analysis the owner decided from.
  - `docs/storage-review.md`: layouts, retention, logs, replay, 5s candles, Dukascopy, cold storage.
  - `docs/hosting-notes.md`: what multi-user hosting would need.
  - `packaging/clean-build.ps1`.
  - Step 1 of it is merged (#13). Keep the documents or fold them into `docs/`; the owner decides.

## Queue

In the owner's order. Each item is one branch from main and stops for review.

1. **Custom indicators**, CI-2 to CI-5 (design: `docs/custom-indicators-design.md`). CI-1 is merged (#17), and so are steps (a), (b) and (c) of CI-2 (#19, #21, #22).
   - **CI-2, steps (d) and (e) (next)**, one reviewable branch each (on open draft PRs when this was written; steps (b) and (c) are merged):
     - **(d)** drawing the new output kinds in the chart;
     - **(e)** `docs/custom-indicators.md` with the four examples and the Pine cheat sheet.
   - **To check in TradingView** (inferred so far, in `docs/decisions.md`): `pivotlow` ties and pivots with several equal bars on the left, `rising`/`falling` for n other than 2, and `rsi` on a window where nothing moved (100).
   - **CI-3** (2 days): indicator instances per chart on the server (D1), with migration from `localStorage`, the menu's Built-in and Custom sections, deleting a definition that is in use.
     - **Backend:** branch `ci-3-chart-indicators` (draft PR): migration 0008, the chart routes, the import, layouts' copies, where-used and delete. **Frontend:** after the other session's chart work merges.
   - **CI-4** (3 days): the browser editor (CodeMirror), New/Edit/Duplicate/Rename/Delete/Import/Export, Save and Run with live preview, versions.
     - **Contract:** `docs/api.md`, "Your indicator definitions" (PR #37).
     - **Constraints from the security review** (`docs/security-review-custom-indicators.md`), to keep in the editor's backend:
       - **request ids on the worker protocol** (S6): every request carries an id the answer echoes; a mismatch is a protocol error (the worker is replaced);
       - **plain files only** (S7): create with an exclusive open, never through or onto a link, junction or folder; the listing skips links;
       - **lowercase ids that match the whole pattern** (S5, S9): file names are created from the id, so `Mine.py` cannot be made, and a lookup compares the listed name exactly (Windows is case-insensitive);
       - **writes atomic** (temporary file in the folder, then a rename), 256 KB cap, nothing outside the indicators folder, `.history/` and `.deleted/`.
   - **CI-5** (1 to 1.5 days): the cache and forming-bar updates, measured against the design's targets.
2. **Storage steps 2 and later**, as decided:
   - **tick retention:** 30 days by default (7/30/90 days, 1 year, forever, off);
   - **M1 depth:** 2 years by default, with 3 months and 1 year as choices;
   - **`purge`** runs VACUUM by default (`--no-vacuum` to skip);
   - **logs:** access log off, 5 rotated service logs kept, compose `max-size 10m` / `max-file 3`.
3. **5-second candles and bar replay**, as separate milestones. 5s candles use the spread/offsets layout and are kept 1 year by default.
4. **More drawing tools:** rectangles, Fibonacci, channels, text labels (SPEC section 10).
5. **More indicators,** indicator alerts, multi-timeframe indicators (deferred by the owner).
6. **Multi-source providers:** MT5, cTrader. Dukascopy is opt-in, for personal installs only, never bundled or hosted; revisit it with this item.
7. **Later (SPEC section 13):** backtester, paper trading, economic calendar, webhook notifier.

## Housekeeping

- **Merged branches still present locally:** `data-folder-lock`, `fixes-after-m8`, `m0-scaffolding` to `m9-second-instrument`, `oanda-request-errors`, `storage-1-candle-format`, `release-0.1.0`, `handoff` and `custom-indicators`.
  - Delete them locally and on GitHub when convenient.
  - Remove their worktrees with `git worktree remove` (see `LOCAL-NOTES.md`).
- **Note on the lock file:** `server.lock` stays in the data folder by design; the lock on it is what counts. So "is the server stopped?" means:
  - nothing listening on the port;
  - no `openchart run` process;
  - no `-wal`/`-shm` files next to the database.

  Not "no lock file". `migrate` refuses (exit code 4) while it is held.
