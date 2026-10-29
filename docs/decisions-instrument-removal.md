# Instrument removal: hidden means hidden

## The bug

Removing an instrument in Settings sets `enabled=0` and stops its feed (M4: hide, keep the
data). `GET /api/instruments` returns every stored instrument with its `enabled` flag, and
the page used the whole list for the selector, the watchlist and the live subscriptions.
Only two places filtered (the first instrument to open, the indicator sync).

Found by reading the code and pinned by tests: the route, the store query, the status and
the feed were already right; the page was not.

## Where the filter goes: the page, not the route

- The route keeps returning everything. It is the documented list, Settings needs the
  removed rows to offer "Add back", and nothing else would use a `?enabled=true` option.
- The page keeps two lists: `instruments` (enabled: selector, watchlist, subscriptions,
  the instrument to open) and `everything`, used only to look up decimals for alerts and
  drawings that belong to a removed instrument. A single filtered list would show those
  alerts with 5 decimals.
- The rules are small functions in `frontend/src/instruments.ts`, tested without the DOM.

## What a hidden instrument keeps doing

- Candles, closures, tick files, drawings and alerts stay stored. `/api/candles` and
  `/api/drawings` still answer for it.
- Not in `/api/status`, the feed, the websocket status or the watchlist.
- **Its active alerts cannot fire**: no feed, no ticks. They stay `active` and the engine
  catches up when the instrument is added back. Settings, Removed says so
  ("N active alerts paused until it is added back"), so a silent dormant alert is visible.

## Settings, Removed

Removed instruments are listed with an estimate of their stored size and an "Add back"
button (the normal add: data kept, the gap filled by the usual catch-up; minute history
choice unchanged). The size is the instrument's share of the candle rows times the
database's used pages, plus the real size of its tick files: a scan for exact bytes would
slow the route. Only removed rows are measured.
