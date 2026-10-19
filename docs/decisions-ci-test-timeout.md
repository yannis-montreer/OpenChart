# A timeout for hung tests (2026-10-19)

The Windows 3.13 test job of PR #53 ran `pytest -q` for 40 minutes (the 3.11 job: 12) and was
cancelled by hand. The log had 46 dots and nothing after them: by collection order the 47th test
is `tests/test_alerts_api.py::test_the_alert_state_of_a_line_is_exposed`. The same commit passed
on the push run (9 min) and on the rerun (13 min); in the 40 latest CI runs the job took 5 to 15
minutes. So it is a rare, non-deterministic hang, not a slow runner.

- **Cause not proven.** That test, like most API tests, calls `client.portal.call(...)` and
  `settle()` (`dispatcher.drain`) without a timeout; `receive_until` has one. Something there can
  wait forever. Without a stack dump the exact line is unknown.
- **Fix:** `pytest-timeout` (dev dependency), `timeout = 300`, `timeout_method = "thread"`
  (signals do not exist on Windows). A hang now fails the job in 5 minutes and prints every
  thread's stack, which names the wait. The whole run stops, not just the test: acceptable for
  a hang that should never happen. The slowest tests are far below 300 s.
- **Next:** when it hits again, read the stack and bound that wait.
