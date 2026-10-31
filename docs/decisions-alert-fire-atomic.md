# An alert fire and the alert's state are written together

Branch `alert-fire-atomic`, step 1 after the indicator alerts design (#59). It applies to
**line and level alerts** as they are today; indicator alerts will use the same path.

## What was wrong

`AlertService.handle()` committed the event, then, in a second commit, the alert (state,
counters, status). The regular save (every 30 s) wrote the states of all alerts and the
instruments' `last_eval`, independently of the fires still waiting in the queue. A crash
(power cut, killed process) could therefore:

- **fire twice:** event on disk, alert state from before the cross: the catch-up after the
  restart finds the cross again;
- **invent a cross:** state after the cross on disk, `last_eval` older: the catch-up replays
  minutes the alert had already seen, with a state from after them (a "down" that never
  happened, then a second "up");
- **lose a fire:** the regular save ran while the fire waited in the queue: state after the
  cross and `last_eval` past it on disk, no event: the restart never looks at that minute.

The three tests in `tests/test_alert_fire_atomic.py` show each one on main.

## The fix

- **One step for a fire** (`AlertStore.record_fire`): the event's INSERT and the alert's
  UPDATE run in one call on the database thread (`store.sqlite.atomic`), in a savepoint.
  With one connection shared by every coroutine, two awaited statements are not a
  transaction: another coroutine's commit between them would write the first alone. If
  another coroutine's transaction is already open, the fire becomes part of it and is
  committed with it (or with nothing, after a crash, which the catch-up repairs).
- **`atomic` uses aiosqlite's private `_execute`** (present since 0.17; we require
  0.20). The tests fail if it disappears. The alternative, an idempotency key per fire in a
  new column, needed a migration and a reconciliation on start; not chosen.
- **A fire carries the alert's state as it was right after it** (`Fire.state`, counters).
  Two fires of one alert waiting at once each write their own state; the latest is saved
  by the next regular save (`AlertEngine.saved` marks the alert dirty).
- **Waiting fires hold back the regular save** (`AlertEngine.holding`): their alerts'
  states are not saved, and their instrument's `last_eval` is saved as at most the second
  before the first waiting fire's minute. A crash then leaves a state from before the
  fire, and the catch-up replays its minute: found again, marked late ("restart").
  A clean stop with fires still queued is covered the same way (before, they were lost).
- **The replay skips minutes an alert already observed** (`AlertState.seen_through`, its
  last observation; `AlertEngine.replay_from`, fixed when the catch-up starts): after a
  crash the alert's saved state can be newer than the saved `last_eval`. Whole minutes,
  because a replayed minute's four prices have made-up times. Live ticks are never
  skipped.
- A fire whose write fails stays held until the next start, which replays it.

## Not changed

- The event log, delivery and retries; the text of messages; the API.
- An edit through the API still saves the whole alert (`store.save`).
