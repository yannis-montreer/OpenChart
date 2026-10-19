# The backfill pagination test and the second of the minute (2026-10-19)

`test_backfill_pagination.py::test_single_run_reaches_latest_complete_candle_then_rerun_adds_nothing`
failed once in CI (Windows 3.11, push run of PR #54): `assert len(server.requests) == 1`, got 0.
The same commit passed in its PR run and on rerun; none of the last 100 CI runs had failed on it.

- **A test relying on the clock, not a race in the backfill code.** The second `backfill(...)`
  has `tail_from = last + 60 = stamps[-1]` (the forming candle's minute) and `end = now`; it
  asks the broker only if `tail_from < int(end)`. The test built `stamps` from the clock's
  minute and left `end` to the default `datetime.now()`. In the first second of that minute
  (`int(now) == stamps[-1]`) the backfill rightly sees nothing to ask for, and the test saw
  zero requests. That is correct behaviour for the code (less than a second of the forming
  candle), so the code is unchanged.
- **Why it hit a CI run, and why it reproduced here:** `tests/clock.py` starts the test
  timeline on an exact minute (15:00:00). A test that runs within about a second of that is at
  second :00. Run alone 100 times, iterations 1 and 2 failed (2 of 100); in the full suite it
  needs the test to start in that second, about 1 run in 60 to 100.
- **Fix:** the scenarios take their time as an argument and pass an explicit `end` half a minute
  into the forming candle, with the mock server's clock set to it (`world()`); nothing reads
  the clock again. A parametrized test freezes the clock at 0.0, 0.2, 0.9, 1.0, 30 and 59.9 s
  past a minute and runs the same scenario: it failed at 0.0, 0.2 and 0.9 before the fix
  (the run order of the suite no longer matters).
- **Measured after the fix:** 100 runs alone and 100 with all 12 cores busy, no failure.
