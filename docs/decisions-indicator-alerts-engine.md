# Indicator alerts, step 2: evaluation (decisions)

For `docs/designs/indicator-alerts.md` (sections 2 to 5) and the owner's answers D1 to D7
in `docs/decisions-indicator-alerts.md`. Builds on step 1 (`docs/decisions-indicator-alerts-api.md`).
Code: `alerts/indicator_eval.py` (the rules, no server), `alerts/indicator_service.py`
(the server's bars, built-ins, worker, delivery). Tests: `tests/test_indicator_eval.py`
(fakes and a fake clock), `tests/test_indicator_alerts_server.py` (the fake worker).
Mutation check: `tools/mutate_indicator_alerts.py`.

## Taken

- **Its own loop.** Indicator alerts run in a task of their own (`indicator-alerts`, once a
  second), not in the line alerts' loop: a computation in the worker (up to its 5 s
  deadline) never delays a line alert.
- **When a bar counts as closed:** its end was `CLOSE_GRACE_SECONDS` (2 s) ago, the stored
  bar is `complete`, and prices are known up to its end: the line engine observed (or
  replayed after a gap) a price at or after it, or no trading minute lies between its last
  observation and the end (a quiet minute, the weekend). An instrument waiting for the line
  catch-up (`engine.gaps`) is not evaluated, so the catch-up always reads repaired bars.
- **Live is a replay of one bar.** One code path (`advance`) over every closed bar after
  `last_bar`, from one computation. Values equal live ones bit for bit when the warmup
  holds exactly (SMA, the echo script: tested). For indicators with unbounded memory (EMA,
  RSI), a replay's frame starts earlier than each live frame did, so values can differ in
  the last digits, as between two chart loads; the declared warmup keeps that below 1e-8.
- **Late:** every fire on a bar older than the newest closed one, or found more than 60 s
  after its close. The reason: `worker` after a failure episode, `restart` on the group's
  first run after a start, `wake` after a freeze, `reconnect` otherwise.
- **Touching then leaving the value is a cross** (−1, 0, −1 fires down on the third bar):
  the line rule, unchanged, as the design says. Onto the value is not.
- **NaN, infinite or non-numeric values are no observation** and clear `prev`; a result
  without the output, or not one value per bar, puts that alert in error (one message
  when its episode starts).
- **One fire per alert and bar**, `INSERT OR IGNORE` on the unique index of step 1; the
  fires and states of one group's run are written in one `atomic` step. A fire already
  logged (an older crash) is neither logged nor sent again.
- **Failure episodes:** a failing group keeps `last_bar`, its alerts carry `error` and
  `error_since`, it is tried again after 10 s; one message when the episode starts, one
  when it ends (with the number of late fires). A quarantined file is an error until the
  file changes (its new hash re-arms the alerts). Script timeouts count toward the
  quarantine as for charts (verified: three timeouts quarantine the file).
- **Watchdog:** a group whose last bar lags the newest closed bar by more than 2 bars (at
  least 5 min) while the market is open is "behind": one message at the start, one at the
  end, none while a failure episode already said why. `state.behind` is in the alert's JSON.
  The start message counts the alerts on indicators.
- **Priority (D1):** `IndicatorRunner` now has three levels: charts, alerts, checks.
- **Warnings:** `lookahead`, `warmup_short`, `unchecked` from the file's checks (refreshed
  on every run), `warmup_cut` when the declared warmup exceeds 20,000 bars. A lookahead
  script is evaluated (D4); its fire message says replayed bars may differ from live ones.
- **D5:** each run compares the fires of the last 24 hours whose bar is in its frame with
  the bar's values now. When the condition would not hold on them, the event's
  `correction` records the new values and the time, once; the fire stands, the bar is not
  evaluated again, and a websocket `alert_event` carries the updated event.
- **D6 and D2:** every chart write (`ChartIndicators.replace`: PUT, import, layout apply,
  deleting a definition) re-checks the chart's indicator alerts. A removed instance stops
  its alerts, settings kept, reason "the indicator was removed from the chart", one
  message per write. New value inputs (not colours or widths) or a new file version
  re-arm; an output that no longer fits stops the alert with the reason. A file edited on
  disk without a chart write is found on the group's next run, by its hash.
- **An edit during a run wins:** an alert edited, stopped or deleted while its group was
  computing is left to its next run (a generation per alert).
- **Weekends:** a bar with no trading (by the instrument's trading clock) is skipped
  without a computation, but only when it is the one bar that closed since the last run;
  after a sleep across it, the bars before it are evaluated first (tested).
- **Catch-up cap:** 7 days, as for lines; older bars are skipped with one message.

## The layout-apply uid fix of #61 (not touched)

Today, applying a layout keeps an instance's uid only when the layout holds the same uid.
With this engine that means:

- an alert on an instance the layout does not have (added after the layout was saved, or
  saved from another chart) is **stopped** by the apply, with "the indicator was removed
  from the chart" and one message;
- an instance kept with other values is **re-armed**.

The fix proposed in `docs/decisions-layout-instance-uids.md` (match by kind and definition
before replacing) would keep those alerts **re-armed instead of stopped** when the layout
has the same indicator, and its `alerts_stopped` list would be exactly the alerts this
engine stops on that write (`IndicatorEvaluator.instances_changed` can return them). No
engine change is needed for it.

## Not done here

- The definition-delete 409 does not count indicator alerts yet (the design's step IA-3);
  deleting with `confirm` stops them through the chart write.
- If deleting a definition fails half-way, its undo puts the chart back, but alerts the
  first write stopped stay stopped (they can be started again).
- The dialog, the legend bell and the Alerts panel (IA-4).
