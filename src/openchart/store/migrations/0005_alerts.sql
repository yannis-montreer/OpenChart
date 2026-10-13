-- Alerts (SPEC section 9), M6. The base tables come from 0001.
ALTER TABLE alerts ADD COLUMN name TEXT;            -- the message, e.g. "XAU_USD, 4h Crossing ray"
ALTER TABLE alerts ADD COLUMN level REAL;           -- level_cross only
ALTER TABLE alerts ADD COLUMN expires_at INTEGER;   -- NULL: never
ALTER TABLE alerts ADD COLUMN notify_json TEXT;     -- channels, e.g. ["telegram", "app"]
ALTER TABLE alerts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'; -- active|triggered|expired|stopped
ALTER TABLE alerts ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alerts ADD COLUMN last_fired_at INTEGER;
ALTER TABLE alerts ADD COLUMN fire_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE alert_events ADD COLUMN line_value REAL;
ALTER TABLE alert_events ADD COLUMN cross TEXT;      -- up | down
ALTER TABLE alert_events ADD COLUMN late TEXT;       -- NULL, or why it was found late: wake | reconnect | restart
ALTER TABLE alert_events ADD COLUMN detected_at INTEGER;
ALTER TABLE alert_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert_events ADD COLUMN error TEXT;      -- last delivery error
CREATE INDEX alert_events_ts ON alert_events (ts);
