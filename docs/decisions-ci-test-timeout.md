# A timeout for hung tests (2026-10-19)

The Windows 3.13 test job of PR #53 ran `pytest -q` for 40 minutes (the 3.11 job: 12) and was
cancelled by hand. The log had 46 dots and nothing after them: by collection order the 47th test
is `tests/test_alerts_api.py::test_the_alert_state_of_a_line_is_exposed`. The same commit passed
on the push run (9 min) and on the rerun (13 min); in the 40 latest CI runs the job took 5 to 15
minutes. So it is a rare, non-deterministic hang, not a slow runner.

- **Cause found (reproduced on this PC, 2026-10-19).** Run alone with `pytest-repeat`, the test hung
  after 1 to 8 repetitions in the first batch. The stack dump of the new timeout put the loop thread
  in `Dispatcher.drain` (`while self.tasks: await gather(*self.tasks)`); a temporary print showed
  the set held one `_system` task that was **done** and never discarded, so the loop never ended.
  A cancel does not end it either: `gather` over finished tasks swallows it, so `fail_after` and
  `wait_for` did not help. Why a finished task stays listed (its `add_done_callback(tasks.discard)`
  did not take effect) is not explained; `drain` now only waits for tasks that are not done.
  `drain` is used by tests only (and `close`), so the product is unchanged.
- **Fix:** `pytest-timeout` (dev dependency), `timeout = 300`, `timeout_method = "thread"`
  (signals do not exist on Windows). A hang now fails the job in 5 minutes and prints every
  thread's stack, which names the wait. The whole run stops, not just the test: acceptable for
  a hang that should never happen. The slowest tests are far below 300 s.
- **Bounded helpers:** `tests/bounded.py::call(client, fn, *args, timeout=30)` is `portal.call`
  with a deadline and the call's name in the failure. Used by the three most-used helpers
  (`push` and `settle` in `test_alerts_api.py`, `push` in `test_instruments.py`). The other
  uses (names in the PR) are left as they are.
- **Measured:** the suite's slowest test is 23.6 s (`test_worker_windows.py::test_a_worker_becomes_ready_when_every_core_is_busy`); the next ones are 12.6 s and below, so 300 s is far
  away. The target test: 150 runs alone and 150 with all 12 cores busy, no failure after the fix;
  before it, a hang within the first 50.
- **Next:** find why the discard callback did not remove the finished task (a test-only
  concern until the dispatcher's tasks matter elsewhere).
