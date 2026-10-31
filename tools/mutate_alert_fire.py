"""Mutation check of the atomic alert fire (docs/decisions-alert-fire-atomic.md): each mutant
breaks one rule in a copy of `src/`, and the tests must fail on it. Same runner as
`mutate_checks.py`:

    python tools/mutate_alert_fire.py                    # every mutant
    python tools/mutate_alert_fire.py "two steps again"  # only the ones named

Exit code 0 when every mutant is killed, 1 when one survives, 2 when one no longer applies
or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_alert_fire_atomic.py",
    "tests/test_alert_engine.py",
    "tests/test_alerts_api.py",
]
E = "openchart/alerts/engine.py"
S = "openchart/alerts/service.py"
Q = "openchart/store/sqlite.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    one(
        "two steps again",
        S,
        "        event = await self.store.record_fire(\n"
        "            event, alert, state, fire.fire_count, fire.last_fired_at\n"
        "        )\n",
        "        event = await self.store.add_event(event)\n"
        "        await self.store.save(alert, touch=False)\n",
    ),
    one("no rollback of a half write", Q, '            db.execute("ROLLBACK TO atomic")\n', ""),
    one(
        "a waiting fire's state saved",
        S,
        "if i in engine.alerts and i not in held]",
        "if i in engine.alerts]",
    ),
    one(
        "last_eval saved past a waiting fire",
        S,
        "last = {k: min(v, before.get(k, v)) for k, v in engine.last_eval.items()}",
        "last = dict(engine.last_eval)",
    ),
    one(
        "the replay repeats what the alert saw",
        E,
        "            if late is not None and t < self.replay_from.get(alert.id, t):\n",
        "            if False:\n",
    ),
    one(
        "the replay skips one minute too few",
        E,
        "a.id: int(a.state.seen_through) // 60 * 60 + 60",
        "a.id: int(a.state.seen_through) // 60 * 60",
    ),
    one(
        "a fire carries the live state",
        E,
        "late, replace(st), alert.fire_count",
        "late, st, alert.fire_count",
    ),
    one("a written fire not saved later", E, "        self.dirty.add(fire.alert.id)\n", ""),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
