# Roadmap

What is merged, what is parked on a branch, and what comes next. Decisions behind each
item are in `docs/decisions.md` (and the `docs/decisions-*.md` files named below). Updated 2026-10-19, to main at #52 (release 0.2.0 prepared in a draft PR).

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
| CI-2 step (d), the output kinds drawn in the chart; the guide, the edit-redraw and stale-parameters fixes (catch-up); indicators computed on the forming bar; `/api/candles` ends with the forming bar; review findings and docs fixes | #24, #26, #28, #29, #31, #32, #34, #35 |
| Custom indicators CI-3: indicators per chart on the server (migration 0008), the picker's Built-in and Your files sections, the import, layouts, API version 2 | #33, #36, #38 |
| Custom indicators CI-4: the API contract, the editor's write endpoints (request ids, S6), the browser editor | #37, #39, #41, #42 |
| Hardening after the security review (S1, S2, S3, S5) | #40 |
| Custom indicators CI-5: values cache, forming bar once a second, read cap (S4) | #46 |
| Review findings: contradictory tip, countdown NaN, CI-3 docs | #43, #44, #45 |
| Bar cache for the read path (`docs/decisions-bar-cache.md`) | #49 |
| More drawing tools: vertical line, rectangle, channel, Fibonacci, text, measure (`docs/decisions-drawing-tools.md`) | #48 |
| Built-in indicators: SMA, RSI, MACD, Bollinger, ATR, Stochastic, tick volume (`docs/decisions-builtin-indicators.md`) | #47 |
| Storage step 2: tick retention, minute history per instrument (migration 0009), purge with VACUUM, log limits (`docs/decisions-retention.md`) | #50 |
| Docs up to date with main (roadmap, README, CI design note) | #51 |
| Mutation tools repaired, two concurrent-write tests, forming-bar test on one fake clock (`docs/decisions-mutation-tools.md`) | #52 |

**v0.1.0 is released as a pre-release** (owner, 2026-10-15). **v0.2.0** is prepared (version, release notes, README) in a draft PR; the owner merges it, then tags `v0.2.0` and publishes the draft release.

## Parked on branches (not merged)

- **`storage-review`** (local, not pushed): the storage analysis the owner decided from.
  - `docs/storage-review.md`: layouts, retention, logs, replay, 5s candles, Dukascopy, cold storage.
  - `docs/hosting-notes.md`: what multi-user hosting would need.
  - `packaging/clean-build.ps1`.
  - Step 1 of it is merged (#13). Keep the documents or fold them into `docs/`; the owner decides.

## Queue

In the owner's order. Each item is one branch from main and stops for review.

1. **Custom indicators** (design: `docs/custom-indicators-design.md`): CI-1 to CI-5 are all merged (see the table above). What is left of it:
   - **To check in TradingView** (inferred so far, in `docs/decisions.md`): `pivotlow` ties and pivots with several equal bars on the left, `rising`/`falling` for n other than 2, and `rsi` on a window where nothing moved (100).
   - **The editor and the security review:** the constraints (S5 to S7, S9) are in `docs/security-review-custom-indicators.md`; the editor's backend (#41) and frontend (#42) are merged. The editor was built against a mock of the contract first; its trial against the real backend is the owner's field test.
2. **Storage steps 2 and later:** step 2 is merged (#50: tick retention 30 days by default, M1 depth 2 years by default, `purge` with VACUUM, log limits). Later steps are in `docs/storage-review.md` (parked below).
3. **5-second candles and bar replay**, as separate milestones. 5s candles use the spread/offsets layout and are kept 1 year by default.
4. **Drawing tools:** the first set is merged (#48). Further tools (SPEC section 10) as the owner asks.
5. **Indicators:** the first set is merged (#47). Still deferred by the owner: indicator alerts and multi-timeframe indicators.
6. **Multi-source providers:** MT5, cTrader. Dukascopy is opt-in, for personal installs only, never bundled or hosted; revisit it with this item.
7. **Later (SPEC section 13):** backtester, paper trading, economic calendar, webhook notifier.

## Housekeeping

- **Merged branches still present:** locally `docs-after-verify` and `mutation-tools-fix` (each with a worktree); on GitHub most of the milestone, CI and fix branches of #1 to #52.
  - Delete them when convenient; the exact commands were given with the release 0.2.0 PR.
  - Not merged, keep: `storage-review` (parked, below). `tmp-pr46` is a local-only merge commit of an old session; the owner decides.
- **Note on the lock file:** `server.lock` stays in the data folder by design; the lock on it is what counts. So "is the server stopped?" means:
  - nothing listening on the port;
  - no `openchart run` process;
  - no `-wal`/`-shm` files next to the database.

  Not "no lock file". `migrate` refuses (exit code 4) while it is held.
