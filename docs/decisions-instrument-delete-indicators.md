# "Delete data…" also deletes the chart's indicators

Branch `instrument-delete-indicators`, step 2 after the indicator alerts design (#59).
Until now the delete left the instrument's `chart_indicators` rows and its
`chart_indicator_revisions` row behind: an instrument added back after a delete found its
old indicators on its chart.

- **Always deleted with the data**, not only with the "drawings and alerts" box: they are
  the chart's settings, and an indicator alert that refers to one is an alert of the
  instrument, which the box already guards (the delete is refused until alerts are named).
  So an alert can never be left pointing at a deleted instance.
- **In the preview:** `stored.indicators` (instances on the chart); the page lists
  "N indicators on its chart". They do not set `needs_drawings_and_alerts`.
- **In the token:** the instances (position, uid, definition, values, display flags) and
  the revision. The revision alone would do (every chart write moves it on); the rows are
  kept for a readable fingerprint. The mutation check marks "instances alone" as
  equivalent for that reason.
- **The charts' lock** (`Server.charts_lock`, formerly made by the app for
  `ChartIndicators`) is held by the delete from the token's check to the end, taken after
  its other locks. Nothing that holds it waits for the instrument, feed or cache locks, so
  no deadlock. A chart write meanwhile waits, also through the VACUUM (seconds, rare).
- **`openchart purge`** shares the deletion, so the command deletes them too; its output
  is unchanged (the counts are in `deleted`).
- **Indicator alerts** (not built yet) follow the design, section 5: counted in `alerts`,
  in the token, refused unless named, deleted with the instrument; another instrument's
  untouched. A test pins this with alert rows of type `indicator`.
- Not done: no `chart_indicators_changed` broadcast after the delete (the chart of a
  removed instrument is not open anywhere; the instrument is gone after it).
