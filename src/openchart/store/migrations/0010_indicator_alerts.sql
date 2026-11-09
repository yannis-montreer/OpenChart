-- Alerts on indicators (docs/designs/indicator-alerts.md section 6,
-- docs/decisions-indicator-alerts-engine.md). Line and level alerts write none of these.
--
-- alerts.condition_json (from 0001, unused so far) holds an indicator alert's instance
-- snapshot and condition; its state_json holds the indicator evaluation state.
ALTER TABLE alerts ADD COLUMN stop_reason TEXT;        -- why it was stopped; NULL when not

ALTER TABLE alert_events ADD COLUMN bar INTEGER;       -- indicator alerts: start of the closed bar
ALTER TABLE alert_events ADD COLUMN value REAL;        -- the output's value on that bar
ALTER TABLE alert_events ADD COLUMN other_value REAL;  -- the number or other output it crossed
ALTER TABLE alert_events ADD COLUMN correction_json TEXT; -- the bar's values after the broker's candles replaced it, when they contradict the fire
-- One fire per alert and bar: a replay after a crash cannot log a bar twice. Line events
-- have no bar (NULL), and NULLs never collide in a unique index.
CREATE UNIQUE INDEX alert_events_bar ON alert_events (alert_id, bar);
