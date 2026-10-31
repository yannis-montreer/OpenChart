# Deleting a removed instrument's data from Settings

Branch `instrument-delete`, after #56 (hidden instruments, the Removed section). Removing an
instrument hides it and keeps everything (M4). Until now the only way to free that space was
`openchart purge` with the server stopped. Settings, Removed now has **Delete data…** beside
**Add back**.

## One deletion, two callers

- `openchart.purge.delete_instrument` deletes the instrument's row, candles (M1, H1), cached
  4h/D/W bars, closures, `cache_dirty` ranges and tick folder; with `drawings_and_alerts`
  also its drawings, its alerts and their alert log. Then `VACUUM` and
  `wal_checkpoint(TRUNCATE)`, unless told not to. `openchart purge` and the server both call
  it; the command's messages and `--no-vacuum` are unchanged.
- The command still refuses while a server holds the data-folder lock (`exclusive`,
  unchanged). The server calls the function itself, inside its own locks (below).
- **VACUUM in the server.** The server has one SQLite connection, shared by every coroutine.
  VACUUM fails inside another coroutine's open transaction or beside an unfinished
  statement, so `purge.compact` tries again every 50 ms for up to 5 s. If it still cannot,
  the rows are already gone and the answer says "not compacted" with the reason: SQLite
  reuses the freed pages, as after `--no-vacuum`. While VACUUM runs (seconds on a large
  file), every other database call of the server waits; the feed keeps its ticks in memory
  and writes them afterwards. A delete is a rare, deliberate act, so this was accepted
  rather than skipping VACUUM.
- Drawings and alerts are deleted by the ids read just before, not by instrument: one
  created during the delete is kept, never lost without the engine knowing.

## The route

- `GET /api/instruments/{i}/delete-preview` and `POST /api/instruments/{i}/delete`
  (`docs/api.md`). Both are under `/api/`, so the login and the same-origin check apply (the
  middleware); no editing gate, since nothing here is code.
- **Refused (409):** an enabled instrument; one the feed still downloads or serves (the
  moment between a remove's database write and its feed rebuild); one whose instrument
  lock is held, named in the answer ("being added", "repair before the alert catch-up",
  "the daily retention", "being deleted"...). A delete never queues behind a lock: it says
  why and the owner tries again.
- **Drawings and alerts** must be named (`drawings_and_alerts: true`) when there are any;
  otherwise 409. Drawings and alerts of other instruments never block.
- **The typed name** (`confirm`) must equal the instrument exactly (400). The page checks it
  too; the server does not trust the page.

## The preview token

- The token is a SHA-256 of what the delete would remove: the instrument's row; count,
  oldest and newest of its M1, H1 and cached bars; its closures; each tick file's path,
  size and modification time; its drawings' and alerts' ids and update times; the count
  and newest id of its alert log.
- The delete recomputes it **inside the locks**, just before deleting, and refuses (409,
  "changed since the preview") on any difference. So a newer candle, a new tick file, a
  new drawing or alert, or an alert that fired since the preview cannot be deleted by a
  confirmation that did not show it. The page then shows the fresh preview.
- No expiry and no server secret: the token is not an authorisation (the session is); it
  only proves that the state is the one previewed. Identical state after a restart gives
  the same token, which is right.
- A rewrite of an existing minute with the same count and range (a repair) does not change
  it. That is not newer data, and the delete removes it anyway.

## Locks: what keeps the feed, the cache and the alert engine away

- **`Server.instrument_locks`** (`purge.InstrumentLocks`): one asyncio lock per instrument,
  with what holds it. Held by adding an instrument (back), removing it, changing its
  minute history, the repair before an alert catch-up, the daily retention's minute purge,
  and the delete. An add-back during a delete waits for it and then downloads afresh.
- **The feed:** the delete also holds the server's reconfigure lock, so the feed cannot be
  rebuilt (with this instrument or otherwise) until the delete is done. The instrument is
  not in the feed (checked inside the locks), so the feed has nothing of it to write.
- **The cache:** `CandleCache.holding` takes the instrument's three timeframe locks. Without
  it a bar computed from the old rows could be written after the delete, and `write_cache`
  creates a series when there is none, which would bring the instrument's series back with
  orphan bars. The concurrency test reads the instrument's 4h bars throughout the delete.
- **The alert engine:** `AlertService.catch_up` now repairs only instruments the feed
  serves, under their lock. Before, a catch-up waiting for an instrument removed meanwhile
  would download into it again (`gap_fill`). After a delete, the engine forgets its deleted
  alerts and lines, its clock and any waiting catch-up; the pages are told (`alert_changed`
  and `drawing_changed`, op `delete`).
- Lock order: instrument, then reconfigure, then cache. The add route takes instrument,
  then reconfigure (inside `reconfigure()`), the same order.

## Settings

- **Delete data…** on each removed row opens a confirmation over the settings dialog: what
  goes, one line per kind with counts (minute and hourly candles, cached bars, closures,
  tick files with their size), the estimated total size (the #56 estimate), a checkbox for
  the drawings and alerts (off by default, shown only when there are some), and a field
  for the instrument's name. **Delete** is live only on the exact name, and with the box
  ticked when there are drawings or alerts.
- A 409 shows the server's reason and reloads the preview (new token, new counts). After a
  delete, the result line says what went and how much the database shrank.
- The texts and rules are plain functions in `frontend/src/instruments.ts`, tested without
  the DOM; the dialog is `frontend/src/ui/deleteInstrument.ts`.

## Tests and checks

- `tests/test_instrument_delete.py`, written first and shown failing (17 of 18; the 404 test
  passed already because the route did not exist). The preview; the delete and its
  leftovers; add again afresh; the typed name; enabled, downloading (a gated fake download
  with the row disabled underneath) and held-lock refusals; drawings and alerts named;
  the catch-up repairing only feed instruments under the lock; the token against a new
  drawing, a new tick file and a newer minute; every other row of every table and every
  other tick file unchanged byte for byte (gold candles, drawings, alerts, a layout,
  settings); the feed streaming and writing gold while the instrument's bars are read
  during the delete and its VACUUM; the locks held through the VACUUM; two deletes at once;
  the command using the shared function.
- `tools/mutate_instrument_delete.py`: 24 mutants of every rule above (refusals, token
  parts, each lock, the catch-up, what is deleted, VACUUM in both callers). The first run
  left two survivors, both test gaps: "token blind to drawings" (the test always added a
  drawing with an alert, and the alert alone changed the token) and "every alert log
  deleted" (no other instrument had an alert log). The tests now add a lone drawing and a
  gold alert event; both mutants are killed. 24 of 24.
- `tests/test_worker_runner.py::test_a_job_past_its_deadline_is_killed_and_a_new_worker_starts`
  fails on this PC on unchanged `main` too (the kill takes about 3.1 s against a 2.5 s
  limit): not this branch.
- Frontend: `instruments.test.ts` (the lines, the size, the typed name and the box, the
  result text). Checked in a browser on a scratch server with the fake broker (port 8771,
  synthetic data): a stale preview refused and reloaded, then the delete with 2 drawings.

## Not done

- `openchart purge` keeps its own wording and its `--drawings-and-alerts` flag; it does not
  use the preview token (it is one command run with the server stopped).
- The instrument's entry in the `m1_checked_through` setting (the last day checked against
  the broker) is left as is: settings are not touched. It is harmless: it only skips
  checking a day already checked, and an instrument added back after a delete downloads
  that day anyway.
