-- What each indicator alert fire was decided on (docs/decisions-indicator-alert-gaps.md):
-- the evaluated difference on the bar and on the bar before it, and the first bar of the
-- computation that gave them, so live and replay can be compared bit for bit. Line events
-- write none of these.
ALTER TABLE alert_events ADD COLUMN observed REAL;       -- output - threshold (or 0/1 for a signal) on the bar
ALTER TABLE alert_events ADD COLUMN observed_prev REAL;  -- the same on the bar before (NULL: none)
ALTER TABLE alert_events ADD COLUMN frame_start INTEGER; -- the first bar the computation read (its anchor's warmup)
