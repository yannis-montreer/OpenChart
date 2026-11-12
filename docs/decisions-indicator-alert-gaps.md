# Indicator alerts: the engine's gaps (decisions)

Follows `docs/decisions-indicator-alerts-engine.md` (step 2, #65) and closes what it left
open, plus three rules found missing. Backend only. Tests: `tests/test_indicator_alert_gaps.py`
(each was shown failing on main first), the five tests of #61 in
`tests/test_layout_instance_uids.py`. Mutation check: `tools/mutate_indicator_alert_gaps.py`
(24 mutants: 23 killed, 1 equivalent, explained in the tool); the step 2 tool
(`tools/mutate_indicator_alerts.py`) still passes, with its stop mutant moved to the new code.
Migration: `0011_indicator_alert_evaluation.sql` (the next free number today; it takes the
next free one at merge if another lands first).

## 1. Deleting a definition

- **The 409 counts the indicator alerts on it**, any status: `usage` has `alerts` (id,
  name, instrument, uid, status), `in_use` is also true when only an alert uses it (its
  instance already removed), and the message says how many and names them.
- **Refused unless they are named, exactly**: `DELETE /api/custom-indicators/{id}?confirm=true&alerts=<id>&alerts=<id>`.
  A missing or an extra id is a 409 that says which. Like the instrument delete, which
  needs `drawings_and_alerts` when there are some.
- **Active ones stop** with "its indicator "X" was deleted", settings kept, one message
  for all of them. Stopped, triggered or expired ones keep their status and reason.
- **All or nothing.** The charts' lists, the layouts and the alerts' stops are written in
  one transaction (`ChartIndicators.commit`, `store.sqlite.atomic`), then applied to the
  alerts in memory, then announced (broadcasts, messages). A failure anywhere in it leaves
  every chart, layout and alert as it was, in the database and in memory, announces
  nothing, and the files move back (as before). Tested with the database refusing the
  layout write after the charts and alerts were written in the transaction.
- Every chart write now goes this way (PUT, import, layout apply, delete): the chart and
  what it does to the alerts are one step. Runs of the evaluator in progress leave the
  alerts concerned alone (their generation moves before the write), so a run never writes
  an older state over a stop; if the write fails, that run's result for them is lost and
  their next run evaluates them again.

## 2. Alerts that cannot be evaluated

Four causes, each an **error on the alert** (`state.error`, the reason in words, in the
API and the Alerts panel's data), the alert stays `active`, and nothing of it is
evaluated until it is fixed:

| Cause | Reason shown |
|---|---|
| The stored condition is incomplete or unreadable (by hand, an older version) | "its condition is incomplete: the stored condition has no output, condition" |
| The definition is gone (a built-in removed, a file deleted outside OpenChart) | "the indicator X no longer exists" |
| Its output is gone or no longer fits (same version string, e.g. a built-in changed by an upgrade) | "its condition no longer fits the indicator: RSI has no output 'x' ..." |
| Its instance is no longer on the chart (a row gone without a chart write) | "its indicator instance u1 is no longer on the XAU_USD chart" |

- **Reported once.** The watchdog's check (every 15 s) sends one message per alert and
  reason not yet named by a message; a failing group's own message counts for its alerts,
  so nothing is said twice. It also runs without a price feed.
- Errors, not stops: the owner asked for an error state. A chart write that removes an
  instance still stops its alerts (D2); the instance check catches the cases where that
  write never happened. An alert in error does not keep its group computing every second:
  it waits for the next closed bar like the others.
- Before, an incomplete condition was skipped silently, and an output missing from the
  definition but present in the result crashed the group's run every second.

## 3. Applying a layout (the fix proposed in #61)

- **Matching**, in this order: the chart instance with the entry's uid **and the same
  definition** (kind and id); else the first unmatched chart instance, in legend order,
  with the same definition **and the same value inputs** (colours and widths left out).
  A matched entry takes the chart's uid: its alerts stay armed, or re-arm when the
  layout's values differ (by uid only, since values must be equal for the second rule).
- The owner's rule is "by uid, then by definition and parameters"; #61 proposed
  definition only. Same definition with other values is **not** a match: the instance is
  replaced and its alerts listed as stopping.
- **A uid shared with another definition is not a match**, and an unmatched entry never
  keeps a uid that is on the chart: an alert would otherwise follow its uid to another
  indicator (for example an RSI alert on an EMA).
- **Preview:** `GET /api/layouts/{id}/apply-preview?instrument=` lists `alerts_stopped`
  (id, name, uid, reason) and `alerts_rearmed`, changing nothing. `POST .../apply` is
  refused (409, with the preview) unless `stop_alerts=<id>` names exactly the alerts it
  would stop; its answer has both lists. Stopped alerts get the reason "the layout "X"
  applied to the chart does not have its indicator" and one message.
- Of #61's five tests, only the third changed: the same indicator with another uid now
  keeps the chart's uid (it asserted the layout's).
- The frontend's layout menu does not pass `stop_alerts` yet: an apply that would stop
  alerts shows the 409's message. No alert can be made from the UI before IA-4, which
  should show the preview and ask.

## 4. The crossing rule per output kind

The (-1, 0, -1) case:

| Output kind | Rule | (-1, 0, -1) | (-1, 0, 1) |
|---|---|---|---|
| line, area | the line rule, unchanged: a bar on the value is a side | down on the 3rd bar | up on the 3rd |
| histogram | a bar exactly on the value is **no side**: `prev < 0 < s` up, `prev > 0 > s` down, `prev` = the last bar with a side | nothing | up on the 3rd |
| marker | a signal on each bar that is on | nothing | on the 3rd |
| background | a signal when it turns on (off the bar before) | nothing | on the 3rd |

- Histograms (MACD's histogram, volume-like bars) often draw 0 for "no signal", so
  touching the value and going back is not a cross there. Lines and areas keep the line
  rule (a drawn line behaves the same way), as the step 2 decision said.
- **Markers and backgrounds are on or off:** 1 (True) is on, any other number is off (0,
  False, or a -1 a script should not give), NaN is no observation. A background turns on
  from any value that is not on. Before, -1 then 1 did not count as turning on.
- The rule takes the kind of the alert's own output; for `cross_output` that is the first
  output (the second may be a line or a level).
- The D5 comparison uses the same rule (the histogram's side before bars on the value).

## 5. Live and replay decide identically

- **Anchored computations.** A computation now starts on a fixed grid: the bars of one
  cell of `ANCHOR_BARS` (512) bars, with the declared warmup before the cell's start,
  whenever it runs. A bar's value is then the same, bit for bit, whether it was evaluated
  at its close or replayed after an outage. Before, live frames started `warmup` bars
  before each new bar and a replay's frame earlier, so indicators with unbounded memory
  (EMA, RSI) differed in the last digits; a value within 1e-9 of the threshold could fire
  live and not in replay. Measured on main: 287 of 378 fires had another value in replay.
- A replay over several cells is one computation per cell (7 days of 1m bars: 20); live,
  a bar close is one computation (two at a cell's first bar), on up to warmup + 512 bars
  instead of warmup + 1. Arming reads the newest cell only.
- **The event log keeps what each fire was decided on:** `observed` (output minus the
  value, or 1 for a signal), `observed_prev`, and `frame_start` (the first bar the
  computation read), next to `value` and `other_value`. In the event's JSON for indicator
  alerts only; line events are unchanged (byte-identical test).
- **D5 no longer records noise.** Live, main recorded "corrections" on fires whose bars
  had not changed, only because two computations started on different bars. A correction
  is now recorded only when the bar's evaluated value moved by more than 1e-8 (relative)
  from what the fire logged, and the condition no longer holds.
- Tested with EMA on FX-like prices: 96 thresholds within 1e-9 (down to 1e-14) of the
  values seen live, evaluated at every close and in one replay, with 512 and with 16 bars
  per cell: the same fires, values and observations, bit for bit, and no correction.

## Not done here

- The dialog, legend bell and Alerts panel (IA-4), including the layout menu asking before
  an apply that stops alerts and showing `state.error`.
- `docs/api.md` describes the new parameters and fields (`alerts=`, `apply-preview`,
  `stop_alerts=`, `observed`); the owner may want them in the release notes.
