# Indicator alerts, step 1: data model and API (decisions)

For `docs/designs/indicator-alerts.md` (sections 1 and 6) and the owner's answers in
`docs/decisions-indicator-alerts.md`. Backend only: no dialog or panel yet. The evaluation
is the next step (`docs/decisions-indicator-alerts-engine.md`).

## Taken in this step

- **Migration 0010, an extension of `alerts`** (the design's choice, not a table of its
  own): `alerts.stop_reason`; the instance snapshot and condition in `alerts.condition_json`
  (in the schema since 0001, unused so far); `alert_events.bar`, `value`, `other_value`,
  `correction_json`, and a unique index on `(alert_id, bar)`. Line events have no bar
  (NULL), which a unique index never compares, so they are not affected. Tested on
  synthetic databases only. 0010 is the next free number today; it takes the next free
  one at merge if another lands first.
- **`correction_json` is in this migration** although D5's record is written by step 2:
  one migration for the whole feature.
- **The indicator state is in `state_json`**, as `IndicatorState` (`last_bar`, `prev`,
  `armed`, `error`, `error_since`, `warnings`), only for `type="indicator"`. `AlertState`
  (lines) gets no new field: a line alert's `state_json` stays byte-identical.
- **Line and level alerts are byte-for-byte unchanged:** `tests/test_line_alerts_unchanged.py`
  compares, as bytes, the REST answers, websocket messages and rows (the columns from
  before 0010) of a line and a level alert created, edited, fired, stopped and deleted,
  against what main at 9f54eb1 gave (`tests/golden/line_alerts_main_9f54eb1.txt`). The
  indicator fields are only added to an indicator alert's JSON and event JSON.
- **The line engine never observes an indicator alert.** It still holds them, so that
  the start message counts them and `expires_at` expires them as any alert.
- **A signal has no direction:** `direction` must be absent or `cross`, and is stored as
  `cross`.
- **`cross_output` may cross a level output** (RSI crossing its 70 level), not a fill,
  marker or background.
- **The uid is fixed:** an alert stays on its instance; another instance is a new alert.
- **A user's stop has the reason "stopped by you"**, so `stop_reason` is never empty on
  a stopped indicator alert. Line alerts have no `stop_reason` in their JSON.
- **Warnings at creation** come from `CustomIndicators.checks_for` with the instance's
  settings: `lookahead` and `warmup_short` when a check warned, `unchecked` when it is
  pending, not checked, or there is none yet. Built-ins have none. `warmup_cut` is set by
  the evaluation (step 2), which knows the warmup it reads.
- **"Delete data..."**: `stored_summary`, the preview's token and the delete already
  select alerts by instrument, so indicator alerts were counted and deleted. Added: the
  preview's `stored.indicator_alerts`, and the token now also covers an indicator alert's
  name, condition and stop reason (an edit within the same second as the preview no
  longer leaves the token unchanged). Only when the instrument has indicator alerts, so
  other instruments' tokens are what they were. The frontend's preview text does not
  show "(N on indicators)" yet: the dialog step.
- **Not changed:** the layout-apply uid behaviour from #61. What it means for the engine
  is in the step 2 decisions.
