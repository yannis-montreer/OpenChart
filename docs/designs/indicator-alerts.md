# Indicator alerts: design (not built)

Status: proposal for the owner's review. Read: `alerts/` (engine, geometry, service, watchdog, notify), `store/alerts.py`, `api/routes_alerts.py`, `api/chart_indicators.py`, `api/routes_indicators.py`, `indicators/_cache.py`, `_custom.py`, `_checks.py`, `worker/runner.py`, `server.delete_instrument`, `purge.py`, `routes_custom_indicators.py`. **[unverified]** marks what I did not check in code or run. Decisions: `docs/decisions-indicator-alerts.md`.

## 1. What an indicator alert is
- New `type="indicator"` in `alerts`. It names one **instance**: `source`, `instrument`, `uid` (from `chart_indicators`), and a snapshot taken at save: `kind`, `id`, value inputs (style inputs dropped, as in the values cache key), the custom file's digest or the built-in's version, and the `output` name.
- **Conditions:** `cross_value` (output crosses a number), `cross_output` (output crosses a second output of the same instance), `signal` (a `Marker` is on the bar, or a `Background` turns on). Directions `up | down | cross` for the two crossings; none for `signal`.
- **Crossing rule = the line rule** (`engine._check`): `s = a - b` per closed bar, `prev <= 0 < s` up, `prev >= 0 > s` down. A null/NaN on either side is no observation and clears `prev` (like a ray outside its segment).
- **Price type** = the series the indicator is computed on (`bid | ask | mid`, `trigger_price`). **Timeframe** = the alert's own (the chart's at creation), 1m and above only: 1s-30s bars exist only from live ticks and cannot be replayed (D3).
- **Closed bars only** (owner's default): no tick evaluation, `trigger_mode="candle_close"` fixed. Frequencies `once | once_per_bar | repeating` (+ cooldown) as for lines; on closed bars `once_per_bar` = every qualifying bar.
- **Arming** (owner's default, as `put(rearm=True)`): the first evaluation after create, edit, re-enable or re-arm only records `prev` and `last_bar`; it never fires.

## 2. Where it runs
- `alerts/indicator_eval.py`: an async evaluator owned by `AlertService` (the engine stays synchronous, no I/O). Built-ins: `asyncio.to_thread(indicator.run)` in the server. Custom: `server.custom.compute()` (worker, same error/quarantine bookkeeping as charts).
- **Trigger:** a bar of the alert's timeframe ended `CLOSE_GRACE_SECONDS` (2 s) ago and the instrument is not in `engine.gaps`. Bars come from `read_bars` minus the forming bar.
- **One job per group per closed bar:** group = (instrument, timeframe, price, kind, id, digest/version, value inputs). Every alert on that group (any condition, any output) reads one result. Several closed bars pending = still one job (replay, section 3).
- **Priority (D1):** a third level in `IndicatorRunner._take_turn`: alert jobs wait behind a running job and waiting chart requests, ahead of save-time checks. A chart waits at most one job; checks never delay alerts.
- **Budget:** the worker deadline per job (5 s default). Groups of one bar close run in timeframe order (1m first); a backlog never queues duplicates, it widens the next replay.
- **Slow / restarting / quarantined worker:** `WorkerUnavailable` or a timeout leaves `last_bar` where it was; retry every 10 s (`RETRY_AFTER_FAILED_START`). Alert-job timeouts count toward quarantine like any job (same script, same rule). Quarantined: the group is "cannot evaluate" until the file changes; a new digest is an edit (section 5).

## 3. Never silently missing a signal
- Each alert's `state_json` keeps `last_bar` (start of the last evaluated closed bar) and `prev`. A run evaluates every closed bar after `last_bar` up to the newest, oldest first, from one computation: **live is a replay of one bar**, one code path.
- **Catch-up** after restart, sleep, feed outage or worker outage: wait until the engine's line catch-up has repaired the history (it holds the instrument lock), then replay. Fires are `late` with the gap's reason (`restart | wake | reconnect`) or the new `worker`. Cap = `CATCH_UP_MAX_SECONDS` (7 days) as for lines; anything older is skipped with one system message naming the span.
- **No double fire:** state and event written in one transaction, plus a unique `(alert_id, bar)` on events. Today `handle()` commits the event and the alert separately and states are saved every 30 s, so a crash could replay a fired bar (verified in code).
- **Watchdog:** a group whose `last_bar` lags the newest closed bar by more than 2 bars (minimum 5 min) while the market is open is "behind": one message at the start of the episode with the reason (worker restarting, quarantined, script error, timeout), one on recovery with the number of late fires. Never one per bar. The start message counts indicator alerts.
- Broker candles replacing tick-built minutes later (`feed.py`) can change a bar already evaluated; it is not re-evaluated (D5).

## 4. Lookahead and warmup
- Frame = declared warmup (`custom.warmup` / `indicator.warmup`, capped at the route's `MAX_WARMUP` 20,000) + the bars to replay, ending on the last closed bar. Values on the evaluated bars equal the chart's when the declared warmup holds.
- At create and edit the dialog shows `custom.checks_for(params)`: lookahead found, warmup short or unbounded (`ta.cum`), or not checked yet. A warning, never a block; the alert row keeps a "⚠ unchecked/lookahead/warmup" badge from the same data, updated when a check settles.
- Lookahead scripts differ between replay (one computation sees later bars) and live: the warning says so; replay stays one computation (D4).
- **Warmup cut** (needed > 20,000 bars, e.g. long lengths on 1m): the alert shows the route's `warmup_cut` notice; alert and chart compute on different first bars, so unbounded indicators can differ from the chart.

## 5. Lifecycle
- **Value inputs or custom file changed** (new digest), output still exists: re-arm (like a moved line) and tell the panel "re-armed: parameters/file changed". Output gone: `stopped`, reason "output X no longer exists". Style-only change: nothing.
- **Instance removed from the chart** (legend ✕, a layout replacing the list [unverified: whether a layout keeps uids]): the alert is `stopped` with a reason, not deleted (D2); the user deletes it or it stays in the log.
- **Definition deleted** (`DELETE /api/custom-indicators/{id}`): `usage()` counts indicator alerts; the 409 message names them; with `confirm` they stop with their instances.
- **Instrument removed:** same as its line alerts. **"Delete data…":** `stored_summary`, `fingerprint` and `purge.delete_instrument` select alerts by `instrument`, so indicator alerts are counted, in the token and deleted (verified); the preview text adds "(N on indicators)". Found: `purge_instrument` does not delete `chart_indicators` rows (separate fix).
- New field `stop_reason` shown in the Alerts panel and in the Telegram message on stop.

## 6. Data model and API
- Migration `NNNN_indicator_alerts.sql` (next free at merge, 0010 today): `alerts.stop_reason TEXT`; the condition in the existing unused `alerts.condition_json` (`uid, kind, id, params, digest, output, other_output, condition, value`); `alert_events.bar INTEGER`, `value REAL`, `other_value REAL`, unique index on `(alert_id, bar)`. Tested on synthetic databases only.
- REST: `POST/PUT /api/alerts` with `type:"indicator"` and `indicator:{uid, output, condition, other_output?, value?}`; the server fills the snapshot from the instance and refuses outputs whose kind does not fit (400). `GET` adds `indicator`, `checks`, `stop_reason`, `behind`. Websocket: existing `alert_changed`, `alert_triggered`, `alert_event`; no new type.
- Delivery: the existing `Dispatcher` (Telegram, retries, event log); text "EUR_USD 1h RSI(14) crossed up 30.00 at bar close 14:00" with "late" as for lines.
- UI: a bell per legend row (opens the dialog for that instance), filled when an active alert uses it; Alerts panel rows with the indicator, condition, badges and stop reason.

## 7. Tests
- Fake worker (scripted delays, deaths, quarantine), fake clock (`tests/clock.py`), fake broker.
- Replay over N bars equals N live closes, bit for bit (built-in and custom); restart (persisted `last_bar`), sleep (wake gap) and worker-outage catch-up with `late`; DST weeks and the weekend for 4h/D/W bars; a crossing exactly at `s = 0` on a bar boundary; worker killed mid-job (no fire lost, none doubled); hostile outputs (NaN, inf, wrong length, huge); arming never fires; one watchdog message per episode; mutation checks on the arming, crossing, dedup and replay rules (`tools/` mutation runner); the full line-alert suite unchanged.

## 8. Milestones
| Step | Size | Model |
|---|---|---|
| IA-1 migration, store, evaluator for built-ins, replay, dedup, tests | L | Opus |
| IA-2 custom through the worker, priority level, watchdog episodes | M | Opus |
| IA-3 lifecycle (re-arm/stop, definition delete, delete preview) | M | Opus |
| IA-4 dialog, legend bell, Alerts panel badges, docs/api.md section | M | Sonnet |

Decisions for the owner (defaults in `docs/decisions-indicator-alerts.md`): D1 priority, D2 stop vs delete on instance removal, D3 no seconds timeframes, D4 replay of lookahead scripts, D5 broker-candle repaint, D6 re-arm vs stop on a file edit.
