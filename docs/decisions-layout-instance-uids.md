# Does applying a layout keep the indicator instances' uids?

Branch `layout-instance-uids`, step 3 after the indicator alerts design (#59), where this
point was marked unverified. Tests: `tests/test_layout_instance_uids.py` (they pass on
main: they pin today's behaviour). Nothing in the code is changed.

## Answer: only sometimes

`POST /api/layouts/{id}/apply` replaces the chart's list with the layout's copy, and each
instance keeps **the uid stored in the layout** (`ChartIndicators.checked`:
`uid = e.uid or new_uid()`). The chart's own uids play no part. So:

- **Kept** when the layout was saved from this chart and the instance was there then: the
  copy has the same uid. Its values become the layout's (for an alert: a parameter change,
  so it re-arms under the design).
- **Lost** for an instance added to the chart after the layout was saved: it is removed.
- **Lost** when the layout holds the same indicator with the same values but another uid
  (saved from another chart, or written by hand): the chart's instance is replaced.
- A layout without uids gets them once, when it is saved, so applying it again gives the
  same uids. One layout applied to two charts gives both the same uids. A uid is unique
  per chart only, so an alert must name (source, instrument, uid), as the design says.

For indicator alerts, applying a layout would quietly stop every alert on an instance
in the last two cases, even when the chart ends up showing the same indicator.

## Proposed fix (not built, for the owner)

1. **Match before replacing.** In `apply`, give each layout entry the uid of a chart
   instance it matches, if any: first the same uid, then the first unmatched instance
   with the same kind and definition, in legend order. Values still come from the
   layout. Unmatched chart instances are removed, as today.
2. **Say what stops.** The apply answer lists the alerts whose instance is removed
   (`alerts_stopped`), and they stop as the design says (settings kept, one message
   naming the layout). The layout menu asks first when there are any.
3. Tests: the four cases above with the match; an alert kept across an apply of the same
   indicator from another layout; one stopped when its instance is not in the layout.

Recommended with indicator alerts milestone IA-3 (lifecycle), since nothing uses uids
across an apply before then. The alternative, keeping the chart's uid only for an exact
uid match (today), is simpler but stops alerts the owner would expect to keep.
