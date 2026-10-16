# The indicator worker's start under full CPU load

Investigation of 2026-10-16, no code changed by it; the fix is the branch that carries this
file. Fake data and copies only; no broker request of any kind.

## Question

During CI-2 testing the isolated worker did not say hello within the server's 60 s when every
core was busy. What does a start cost, and why?

## Method

- A copy of the interpreter, the packages and the source in a scratch folder, started
  through the real `windows.start` (the real job object, token or AppContainer, pipes),
  running the real `child.py`; time from the call to the hello message, then to the arming.
  The job's CPU accounting gives the CPU the worker used.
- One factor changed at a time from the baseline (restricted token, below-normal priority,
  50%-of-one-core hard cap, thread-limit variables set): priority class, CPU cap, thread
  variables, identity (restricted, job only, AppContainer).
- Idle machine: 5 runs per setting. Full load: one normal-priority busy loop per logical CPU
  (12), 3 runs per setting, 75 s limit. Then 4, 8, 11, 16 and 24 loops for the baseline and
  the normal-priority variant. Warm file cache.
- The restricted runs used the installed interpreter and packages, read-only: a Low
  integrity process cannot start from a copy under the temporary folder.
- Machine: 12 logical CPUs (2 performance and 8 efficiency cores), Windows 10.

## Results (seconds to the hello, median; idle n=5, loaded n=3)

| Setting | Idle | 12 busy loops |
|---|---|---|
| Baseline (restricted, below normal, cap 50%, thread variables) | 3.2 (CPU 1.5) | no hello in 75 s (3 of 3), none in 150 s earlier; 0.1 to 0.4 s of CPU |
| Normal priority only | 2.4 | 6.9 (5.9 to 7.1) |
| No cap only | 1.5 | no hello (3 of 3) |
| No thread variables only | 2.8 | no hello (3 of 3) |
| Normal priority, no cap, no thread variables | 1.1 | 5 to 26 (median 19) |
| Job only (no token) | 2.2 | no hello (3 of 3) |
| AppContainer | 2.3 | no hello (3 of 3) |
| AppContainer, normal priority | 1.9 | 11 to 24 |

Load level, restricted worker, baseline against normal priority: 4 loops 6.1 / 4.2; 8 loops
5.4 / 3.7; 11 loops 2 of 3 failed / 3.7; 16 loops 3 of 3 failed / 8.5; 24 loops 3 of 3
failed / 18.7.

Where the time goes (idle, no cap): the interpreter about 0.1 s, numpy 0.2 to 0.3 s, pandas
0.8 to 1.0 s more, the openchart package 0.06 s: 1.1 to 1.6 s of CPU in all. Setup calls:
job object 0.1 ms, restricted token 0.06 ms, AppContainer profile 10 ms idle and 41 ms loaded
(87 max), private desktop 3 ms, `CreateProcess` 10 to 240 ms.

## Findings

1. **Priority is the cause.** Below-normal threads get almost no CPU next to normal-priority
   work that fills every core. Normal priority alone: from never to about 7 s.
2. **The cap costs time at idle only.** 50% of one core doubles an idle start (1.5 s of CPU
   in about 3 s). It makes no difference under load.
3. **Thread variables, identity and the job setup are not causes** (within the noise at
   idle; below normal fails the same way whatever the identity).
4. **A below-normal worker answers requests at once but wakes late.** With all cores busy,
   requests were answered in 10 to 20 ms (the pipe wakes it) while a graceful exit took
   about 40 s and a timed sleep woke about 40 s late.

## Limits

The Windows service path (LocalSystem, private desktop) cannot be reproduced without a
service; the CI service job asserts it from the service log. Cold-disk starts and Defender's
share were not measured. A single laptop; the CI runners have fewer cores.

## Outcome

Decisions in `docs/decisions.md`, "The worker's start under full CPU load": created at normal
priority and lowered by the server after the hello (read back, fail closed); start deadline
90 s. Considered and deferred: lifting the cap during the start (about 1.5 s at idle).
Rejected: lazy imports, prewarming.
