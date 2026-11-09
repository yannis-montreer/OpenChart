# Indicator alerts: decisions

For `docs/designs/indicator-alerts.md`. D1 to D7 were decided by the owner on 2026-10-31.
Where the owner's answer differs from the recommended default, it says so.

## Status

- Prerequisites merged: #60 (atomic fire, `store.sqlite.atomic`), #61 (layout instance
  uids) and #62 (deleting an instrument's data also deletes the chart's indicators and
  their revision). The design is #59.
- The open question about layout apply and instance uids is answered by #61
  (`docs/decisions-layout-instance-uids.md`).
- **Next: the alert engine work** for indicator alerts.

## Decisions

- **D1 Worker priority of alert jobs.** A level after chart requests and before the
  save-time checks (charts first, alerts next, checks last). Default taken.
- **D2 Instance removed from its chart.** The alert is stopped and kept, with its settings;
  one message says why (the indicator was removed from the chart). Default taken.
- **D3 Timeframes.** No seconds timeframes: 1m and above only (1s to 30s bars come from
  live ticks, and a missed period cannot be replayed). Revisit with the 5-second candles
  milestone. Default taken.
- **D4 Script flagged for lookahead.** Still evaluated, with a visible warning on the alert.
  Replay after an outage is one computation, as for clean scripts, and the warning says
  replay may differ from live. Default taken; the warning being visible on the alert is
  the owner's wording.
- **D5 A bar later replaced by the broker's candle.** It never cancels a fire already sent
  and is not evaluated again. When the corrected value contradicts the fire (the
  condition would not have held on the broker's candle), the event log records that, with
  both values. Evaluation is not delayed to wait for the broker's minute. **Differs from
  the default** only in the event-log record.
- **D6 Custom file or parameters edited while an alert uses it.** The alert is re-armed:
  the first evaluation after the edit records the state and never fires. **Differs from
  the default** by also covering parameter edits. The default's other case is unchanged:
  if the alert's output no longer exists after a file edit, the alert is stopped with a
  reason instead (not covered by the owner's answer; assumed).
- **D7 Wording and limits.** "Behind" after 2 bars (at least 5 minutes); catch-up cap 7
  days, as for lines. Default taken.

## Upgrading aiosqlite

`pyproject.toml` pins aiosqlite exactly (`aiosqlite==X.Y.Z`) because
`store.sqlite.atomic` calls aiosqlite's private `Connection._execute` (see
`docs/decisions-alert-fire-atomic.md`). `tests/test_aiosqlite_contract.py` fails, naming
this section, if the method is missing, if its signature changes, or if the installed
version is not the pinned one. To upgrade:

1. Read the new version's source for `Connection._execute` (and `_conn`, which `atomic`
   also uses): it must still run a callable on the connection's own thread and return its
   result as an awaitable.
2. Change the pin in `pyproject.toml`, reinstall, and run the full test suite on Python
   3.11 and 3.13 (`tests/test_alert_fire_atomic.py` covers the behaviour).
3. If `_execute` is gone or changed, adapt `atomic` in `store/sqlite.py` (for example a
   dedicated thread of our own) rather than loosening the tests.
4. Add a line to the PR description saying which version it moves from and to.
