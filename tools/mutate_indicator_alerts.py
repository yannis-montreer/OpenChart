"""Mutation check of the indicator alert evaluation (docs/decisions-indicator-alerts-engine.md):
each mutant breaks one rule in a copy of `src/`, and the tests must fail on it. Same runner
as `mutate_checks.py`:

    python tools/mutate_indicator_alerts.py                 # every mutant
    python tools/mutate_indicator_alerts.py "no arming"     # only the ones named

Exit code 0 when every mutant is killed, 1 when one survives, 2 when one no longer applies
or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_indicator_eval.py",
    "tests/test_indicator_alerts_server.py",
    "tests/test_indicator_alerts_api.py",
]
E = "openchart/alerts/indicator_eval.py"
S = "openchart/store/alerts.py"
R = "openchart/alerts/indicator_rules.py"
W = "openchart/indicators/worker/runner.py"
V = "openchart/alerts/indicator_service.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    one("no arming", E, "    if not st.armed:\n", "    if False:\n"),
    one("up from zero is no cross", E, "    if prev <= 0 < s:\n", "    if prev < 0 < s:\n"),
    one("down from zero is no cross", E, "    if prev >= 0 > s:\n", "    if prev > 0 > s:\n"),
    one(
        "an evaluated bar evaluated again",
        E,
        "if st.last_bar is not None and start <= st.last_bar:",
        "if st.last_bar is not None and start < st.last_bar:",
    ),
    (  # both checks: either alone still turns NaN and infinity into no observation
        "NaN or infinity observed",
        E,
        [
            ("    if not (math.isfinite(a) and math.isfinite(b)):\n        return None\n", ""),
            ("    return s if math.isfinite(s) else None\n", "    return s\n"),
        ],
        False,
    ),
    one(
        "a background fires while it stays on",
        E,
        '            return "signal" if prev == 0.0 else None\n',
        '            return "signal"\n',
    ),
    one(
        "replayed fires not late",
        E,
        "        late = reason if (start != newest",
        "        late = None if (start != newest",
    ),
    one(
        "no close grace",
        E,
        "        forming = bar_bounds(now - CLOSE_GRACE_SECONDS, tf)[0]  # bars before it",
        "        forming = bar_bounds(now, tf)[0]  # bars before it",
    ),
    one(
        "bars without known prices evaluated",
        E,
        "        return start if self.fresh(instrument, end) else None\n",
        "        return start\n",
    ),
    one(
        "a message per failure",
        E,
        "        if group.failing is None:\n            group.failing = Episode(now, why)",
        "        if True:\n            group.failing = Episode(now, why)",
    ),
    one(
        "no retry pause",
        E,
        "        group.retry_at = now + RETRY_SECONDS\n",
        "        group.retry_at = now\n",
    ),
    one(
        "an edit during a run is overwritten",
        E,
        "if self.generation.get(alert.id, 0) != gens[alert.id] or not alert.active:",
        "if not alert.active:",
    ),
    one(
        "one computation per alert",
        E,
        "        json.dumps(c.params, sort_keys=True),\n    )",
        "        json.dumps(c.params, sort_keys=True) + alert.id,\n    )",
    ),
    one(
        "a new file version not re-armed",
        E,
        'if key.kind == "custom" and resolved.digest != key.digest:',
        "if False:",
    ),
    one(
        "a removed instance leaves the alert active",
        E,
        '                alert.status, alert.stop_reason = "stopped", change.stop',
        "                alert.stop_reason = change.stop",
    ),
    one(
        "a correction recorded although the fire still holds",
        E,
        "            if not contradicts(cond.condition, kind, prev, s, event.cross):",
        "            if contradicts(cond.condition, kind, prev, s, event.cross):",
    ),
    one(
        "the weekend skip over a traded bar",
        E,
        "            and bar_bounds(newest_possible - 1, tf)[0] <= group.evaluated_through\n",
        "",
    ),
    one(
        "a bar logged twice",
        S,
        '"INSERT OR IGNORE INTO alert_events (alert_id, ts, price, line_value, cross,"',
        '"INSERT INTO alert_events (alert_id, ts, price, line_value, cross,"',
    ),
    one(
        "event and state in two steps",
        S,
        "        ids = await atomic(self.conn, write)\n        out: list",
        "        ids = write(self.conn._conn)\n        self.conn._conn.commit()\n        out: list",
    ),
    one(
        "lookahead not shown",
        R,
        '    if checks.get("lookahead") == "warned":',
        '    if checks.get("lookahead") == "never":',
    ),
    one(
        "alerts before charts",
        W,
        'PRIORITIES = ("normal", "alert", "low")',
        'PRIORITIES = ("alert", "normal", "low")',
    ),
    one(
        "checks before alerts",
        W,
        'PRIORITIES = ("normal", "alert", "low")',
        'PRIORITIES = ("normal", "low", "alert")',
    ),
    one(
        # Equivalent: without this branch a quarantined file is still not run and still
        # reported, since `CustomIndicators.compute` refuses it (ScriptFailed
        # "Quarantined: ...") and the alert shows that error. The branch only saves the
        # call and words the error.
        "quarantine not reported",
        V,
        '        if d.status == "quarantined":\n'
        '            return Resolved(None, d.digest, d.status, f"quarantined: {d.reason}")\n',
        "",
        True,
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
