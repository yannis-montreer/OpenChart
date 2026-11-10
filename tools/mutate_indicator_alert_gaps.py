"""Mutation check of the rules added for the gaps of the indicator alert engine
(docs/decisions-indicator-alert-gaps.md): each mutant breaks one rule in a copy of `src/`,
and the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_indicator_alert_gaps.py                 # every mutant
    python tools/mutate_indicator_alert_gaps.py "no anchor"     # only the ones named

Exit code 0 when every mutant is killed, 1 when one survives, 2 when one no longer applies
or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_indicator_alert_gaps.py",
    "tests/test_layout_instance_uids.py",
    "tests/test_hardening.py",
]
E = "openchart/alerts/indicator_eval.py"
C = "openchart/api/chart_indicators.py"
D = "openchart/api/routes_custom_indicators.py"
L = "openchart/api/routes_layouts.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # 1. Deleting a definition
    one(
        "a delete without the alerts named",
        D,
        'if found["in_use"] and (not confirm or set(alerts) != named):',
        'if found["in_use"] and not confirm:',
    ),
    one(
        "a delete that does not count alerts",
        D,
        '"in_use": bool(used or layouts or alerts),',
        '"in_use": bool(used or layouts),',
    ),
    one(
        "alerts stopped in memory before the transaction",
        C,
        "        await atomic(self.server.db.conn, write)\n",
        "        if indicators is not None:\n"
        "            indicators.evaluator.apply_changes(changes)\n"
        "        await atomic(self.server.db.conn, write)\n",
    ),
    one(
        "the alerts written outside the chart's transaction",
        C,
        "            IndicatorEvaluator.write_changes(db, changes)\n\n"
        "        await atomic(self.server.db.conn, write)\n",
        "\n        await atomic(self.server.db.conn, write)\n"
        "        await atomic(self.server.db.conn,"
        " lambda db: IndicatorEvaluator.write_changes(db, changes))\n",
    ),
    # 2. Alerts that cannot be evaluated
    one(
        "an incomplete condition skipped silently",
        E,
        'if alert.type == "indicator" and alert.active and alert.indicator is None:',
        "if False:",
    ),
    one(
        "an output that no longer fits not checked",
        E,
        "            why = output_exists(schema, cond)\n            if why is not None:\n"
        '                why = f"its condition',
        "            why = None\n            if why is not None:\n"
        '                why = f"its condition',
    ),
    one(
        "an instance off the chart not checked",
        E,
        "elif on_chart is not None and cond.uid not in on_chart:",
        "elif False:",
    ),
    one(
        "an error reported at every check",
        E,
        "            if self.reported.get(alert.id) == why:\n                continue\n",
        "",
    ),
    one(
        "an error never reported",
        E,
        "            self.reported[alert.id] = why\n            out.append(",
        "            self.reported[alert.id] = why\n            continue\n            out.append(",
    ),
    one(
        "a missing definition not said to be gone",
        E,
        'if resolved.status in ("missing", "ok") and resolved.schema is None:',
        'if resolved.status == "missing":',
    ),
    # 3. Applying a layout
    one(
        "no match by definition and values",
        L,
        "if c.uid not in taken and same and await values(c) == mine:",
        "if False:",
    ),
    one(
        "a match by uid alone",
        L,
        "if c is not None and (c.kind, c.indicator) == (i.kind, i.indicator):",
        "if c is not None:",
    ),
    one(
        "a match by definition whatever the values",
        L,
        "if c.uid not in taken and same and await values(c) == mine:",
        "if c.uid not in taken and same:",
    ),
    one(
        "an unmatched entry takes a uid on the chart",
        L,
        "uid = i.uid if i.uid not in by_uid and i.uid not in used else new_uid()",
        "uid = i.uid if i.uid not in used else new_uid()",
    ),
    one(
        "an apply that stops alerts without them named",
        L,
        "            if set(stop_alerts) != stopping:\n",
        "            if False:\n",
    ),
    # 4. The crossing rule per kind
    one("a histogram at zero is a side", E, "    if kind in ZERO_IS_NO_SIDE:\n", "    if False:\n"),
    one(
        "a histogram forgets its side on zero",
        E,
        '    return condition != "signal" and kind in ZERO_IS_NO_SIDE and s == 0.0',
        "    return False and s == 0.0",
    ),
    one(
        "signal values not read as on or off",
        E,
        "    return 1.0 if x == 1.0 else 0.0\n",
        "    return x\n",
    ),
    # 5. Live and replay
    one("no anchor", E, "    return t - t % span\n", "    return t\n"),
    one(
        # Equivalent: a frame's warmup bars are before its cell, so they are bars of
        # earlier cells, which this run evaluated first (or which are at or before every
        # alert's `last_bar`): `advance` skips them anyway. The filter keeps that explicit.
        "warmup bars evaluated",
        E,
        "own = [i for i, s in enumerate(starts) if s >= cell]",
        "own = list(range(len(starts)))",
        True,
    ),
    one(
        "arming on the first cell",
        E,
        "if not st.armed and k != len(results) - 1:",
        "if False:",
    ),
    one("the evaluated value not logged", E, "        observed=f.observed,\n", ""),
    one(
        "warmup bars compared for a correction",
        E,
        "if i is None or i == 0 or i < reliable_from or cond is None:",
        "if i is None or i == 0 or cond is None:",
    ),
    one(
        "computation noise recorded as a correction",
        E,
        "                and not moved(event.observed, s, scale)\n",
        "                and False\n",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
