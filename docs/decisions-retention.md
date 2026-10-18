# Storage steps 2: retention, minute history, purge, logs (2026-10-17)

The steps of `docs/storage-review.md` (branch `storage-review`, decisions of 2026-10-14) that
were decided: tick retention, M1 depth per instrument with the daily purge of older minutes,
`purge` with VACUUM, the access log off, five rotated service logs, compose log limits.
Tests first (`tests/test_retention.py`, 65 failed before the code), then the code.

## The rule: nothing newer than the limit is ever deleted

- **One limit for each kind, in UTC seconds:** `cutoff = floor(now) - days * 86400`
  (`retention.cutoff`). Never later than `now - days`, so rounding keeps data, never deletes it.
  Summer time cannot move it: no local time is involved. Tested on a grid of clocks around the
  US and European changes of 2026 and on, just before and just after 00:00 UTC.
- **Minutes:** one goes once it ended at or before the limit (`ts + 60 <= cutoff`). The store's
  `purge_m1_before` deletes exactly what `count_m1_before` counts (the dry run).
- **Tick days:** a UTC day's folder goes once the whole day ended at or before the limit (its
  end, the next 00:00 UTC, `<= cutoff`). A folder that still has part files (not compacted) or a
  name that is not a date is never touched. Deletion runs under the same lock as the flush and
  the compaction (`TickWriter._files`), so it cannot remove a folder being written.
- **H1 is never deleted.** After the minutes go, 1h and longer bars there are built from H1 (as
  the review planned). The purge drops the cached 4h/D/W bars from the oldest deleted minute to
  the hour after the new first minute (the hour where M1 takes over from H1 moves) and they are
  recomputed; a test compares them with a fresh computation. The store's epoch moves before the
  delete and after the commit, as for every other write (a cache's read under way is not kept).

## Choices and defaults

- **Raw ticks:** 7 days, 30 days (default), 90 days, 1 year, forever, "don't store ticks" (the
  old switch). Setting `tick_retention`; `store_ticks` is kept in step with it, and a page that
  still sends `store_ticks` gets "off" or the default.
  **Decided here:** "off" stops storing and deletes nothing, so ticks stored before stay until
  the choice is changed again (deleting on "off" would be a surprise). Open question below.
- **Minute history per instrument:** 3 months, 1 year, 2 years (default). Column
  `instruments.m1_depth` (migration 0009, NULL = default). Days rounded up so a choice is never
  shorter than its name: 92, 366, 731 (`ceil(years * 365.25)`). The default follows
  `OPENCHART_BACKFILL_YEARS` (2), so the download and the purge always agree: the feed's M1
  download now stops at the instrument's limit (`Feed.m1_target`) instead of a global 2 years.
- **Longer depth:** the M1 download's "finished" mark is cleared when the new limit lies before
  where it finished, and the feed restarts (as when an instrument is added) and downloads the
  missing minutes, newest first. **Shorter:** nothing is deleted at once; the next daily run
  does it (the dry run shows it).
- **Disabled instruments** keep all their minutes (only enabled ones are purged). "Removed"
  in Settings means kept, as the page says.
- **`openchart backfill`** now goes back to the instrument's depth by default, and says so when
  `--years` reaches further (those minutes would go at the next daily run).

## When it runs, and the dry run

- **The server** runs it at start and once each UTC day (checked every minute, so just after
  00:00 UTC): `Server.run_retention`. One log line per tick day deleted, one per instrument
  whose minutes were deleted (count, first and last minute, the limit), one for old service
  logs. A failure is logged and retried the next day; it never stops the server.
- **A day of grace on the first start (decided here, the safest option):** the first run on
  an install records when it happened (`retention_since`) and, for 24 hours, deletes nothing:
  it logs a warning with what it would delete and from when. Without it, the first start after
  this is merged would delete old ticks and minutes before the owner could choose "Forever" in
  the new Settings, which only exist once the server runs. Old rotated logs wait too.
- **`openchart retention`** shows what that run would delete today (tick days with sizes,
  minutes per instrument with their range, rotated logs) and deletes nothing. Read-only: it runs
  beside a running server.

## `purge`, logs, compose

- **`purge` runs VACUUM** after deleting, with the size before and after; `--no-vacuum` skips it.
  It already needs the data folder to itself (server stopped), which VACUUM needs too.
- **Access log off:** uvicorn's line per request is off unless `OPENCHART_ACCESS_LOG=1`. Failed
  requests are still logged by the app.
- **Service logs:** NSSM renames a full `service.log` to `service-<time>.log` and never deletes
  any; the daily run deletes rotated ones beyond the newest 5 (by modification time).
  `service.log` itself and other files are never touched.
- **Compose:** both services log with `json-file`, `max-size: 10m`, `max-file: "3"`.

## Settings page

- Data: "Keep raw ticks" is a choice of the six. Instruments: each row shows "Minute history:
  1 year (back to 14 Oct 2025)" and a choice; adding an instrument has the same choice.
- Sizes in the help texts: ticks 17 to 21 MB per instrument a month (measured in the review);
  minutes about 22 MB a year (the review's 50 MB a year before storage step 1, 57% smaller after
  it). Not measured again here.

## Checks

- `tests/test_retention.py`: the invariant on 14 clocks for minutes and tick days and four tick
  limits; deletion and log lines; part files refused; 4h/D/W bars after the purge equal to a
  fresh computation; the epoch before and after; the dry run deletes nothing; the server's run
  at start and its log lines; the day of grace; "forever" and "off" delete no ticks; the API (choices, older pages,
  validation, the depth endpoint, a longer depth clearing the mark); the feed's target; VACUUM
  and `--no-vacuum`; the access log switch passed to uvicorn; five logs kept; compose limits;
  migration 0009 on a synthetic database at version 8 (rows kept, NULL depth).
- Mutation check (`tools/mutate_retention.py`): 29 mutants (the limits and their rounding, the
  tick-day and minute boundaries, part files, the cache invalidation, the epoch, the grace,
  disabled instruments, "off" and "forever", the daily schedule, log pruning, settings from an
  older page, the download target and mark, VACUUM, the access log): 29 killed. Two would have
  survived for lack of a test (disabled instruments, the server pruning logs); tests were added
  before the run rather than marking them equivalent.
- Frontend: `retention.test.ts` (labels, the line, UTC dates). Checked in a browser against a
  fake broker on a scratch port: both choices save and the server reports them; no console
  error. Found there and fixed: the depth choice took the instrument row's whole width.
- Full suite: 1328 passed, 2 failed, 8 skipped, 1 xfailed. Neither failure is this change:
  `test_worker_runner.py::test_a_job_past_its_deadline_is_killed_and_a_new_worker_starts`
  fails on this PC with the machine busy, on other branches too; and
  `test_chart_indicators.py::test_delete_refuses_names_that_are_not_ids[..%2Fmine]` fails
  only when the web UI has been built in the worktree (the static files answer 405 instead of
  the API's 404); it passes with the build moved aside, and on `main`.

## Open questions

1. Should "don't store ticks" also delete the ticks already stored (after the last chosen
   limit)? Now: it deletes nothing.
2. Is a day of grace on the first start enough? The first start after this is merged logs what
   would go and deletes it a day later (tick days older than 30 days, minutes older than 2
   years), unless other limits are chosen in Settings meanwhile.
3. Disabled instruments are never purged. Should they follow their depth too?
