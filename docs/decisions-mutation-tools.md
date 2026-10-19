# Mutation tools: repairs (2026-10-19)

Branch `mutation-tools-fix`. Draft PR; nothing here changes the product code.

- **The two survivors of `tools/mutate_chart_indicators.py` were test gaps, not tool gaps.**
  "A layout saved outside the lock" and "the file looked up only before the lock" had tests
  from the review of CI-3, but the hardening change (the delete moves the files first, the
  instances after) put those tests' pause (inside the chart write) after the point where
  the race happens. Both tests now pause the delete's where-used count
  (`pause_delete_in_usage`: the first read of the saved layouts answers, then waits 1 s),
  which is inside the lock and before anything moves. Shown failing first against each
  mutant (`['mine', 'ema'] == ['ema']`; `[200, 409] == [200, 404]`), passing on the code.
- **Eleven mutants no longer matched the source** and were rewritten to the current text:
  chart_indicators 6 (the import's `checked(entries, ...)`, the editing gate on usage and on
  delete, the history move, the order of removal and move, the `indicators_changed`
  message), checks 2 (the "already checked" early return; the clock read after the worker
  start), hardening 2 (`put_back` is now `await asyncio.to_thread(put_back, ...)`),
  values_key 1 (the epoch bump in `_bump`, which now has two other `+= 1` lines).
  None was left unfixed.
- **One of them needed a new test.** "Instances removed before the files move" (the old
  "before the move is prepared" no longer exists) survived: a failed removal is put back,
  so the list looked the same. The charts' revision does not: a test that makes the file
  move fail now asserts the revision did not move.
- **The runners already refused to start on a failing baseline and exited 2 when a mutant
  no longer applied** (`mutate_checks.main`, shared by all the tools; `mutate_drawing_tools`
  has its own two baselines); no change needed. A check that every mutant's text matches
  exactly once was run over all seven Python tools: none stale now.
- **Subprocess reads** of the shared runner (`mutate_checks.pytest`) pass
  `encoding="utf-8", errors="replace"`, so `mutate_retention.py` (which has no subprocess of
  its own) and the others no longer depend on the console's code page.
- **`test_the_forming_bar_is_recomputed_at_most_once_a_second`:** could not be reproduced as
  flaky (20 of 20 idle, 20 of 20 with all 12 cores busy, before and after). The change is a
  precaution, not a confirmed cure: the cache's clock and the readers' wall clock
  (`market/series.py`, `market/feed.py`) are one fake clock, so only the fake clock decides
  the number of computes. If it fails again, the cause is not the clock.
- **`mutate_drawing_tools.py`'s browser half** needs `npm ci` in `frontend/` first (a fresh
  worktree has no `node_modules`); without it the baseline prints no output and the tool
  exits 2.
