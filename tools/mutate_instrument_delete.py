"""Mutation check of "Delete data..." on a removed instrument
(docs/decisions-instrument-delete.md): each mutant breaks one rule in a copy of `src/`, and
the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_instrument_delete.py                  # every mutant
    python tools/mutate_instrument_delete.py "token ignored"  # only the ones named

Exit code 0 when every mutant is killed, 1 when one survives, 2 when one no longer applies
or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_instrument_delete.py",
    "tests/test_download.py::test_purge_deletes_one_instrument_and_leaves_the_others",
    "tests/test_retention.py::test_purge_runs_vacuum_unless_told_not_to",
]
V = "openchart/server.py"
P = "openchart/purge.py"
A = "openchart/alerts/service.py"
M = "openchart/main.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # Refusals
    one("an enabled instrument allowed", V, "        if row.enabled:\n", "        if False:\n"),
    one(
        "a download not refused",
        V,
        "if feed is not None and name in feed.downloading:",
        "if False:",
    ),
    (
        "neither download nor feed refused",
        V,
        [
            ("if feed is not None and name in feed.downloading:", "if False:"),
            ("if feed is not None and name in feed.instruments:", "if False:"),
        ],
        False,
    ),
    one(
        "a held lock not refused",
        V,
        "holder = None if holding else self.instrument_locks.busy(name)",
        "holder = None",
    ),
    one(
        "drawings and alerts not required",
        V,
        "            if not drawings_and_alerts:\n",
        "            if False:\n",
    ),
    one(
        "the typed name not checked",
        V,
        "        if confirm != instrument:\n",
        "        if False:\n",
    ),
    # The token
    one(
        "token ignored",
        V,
        "if token != await purge.fingerprint(self.store, ticks, source, instrument):",
        "if False:",
    ),
    one(
        "token blind to tick files",
        P,
        'parts["ticks"] = await asyncio.to_thread(_tick_files, ticks_dir / source / instrument)',
        'parts["ticks"] = []',
    ),
    one(
        "token blind to drawings",
        P,
        '"SELECT id, updated_at FROM drawings WHERE instrument=? ORDER BY id", instrument',
        '"SELECT 1 WHERE ?", 0',
    ),
    one(
        "token blind to candles",
        P,
        '"candles": await store.extent(source, instrument),',
        '"candles": [],',
    ),
    # Locks
    one(
        "the instrument's lock not held by the delete",
        V,
        'self.instrument_locks.hold(instrument, "being deleted"),',
        "contextlib.nullcontext(),",
    ),
    one("the feed's lock not held", V, "            self._reconfigure_lock,\n", ""),
    one(
        "the cache's locks not held",
        V,
        "self.cache.holding(source, instrument),",
        "contextlib.nullcontext(),",
    ),
    one(
        "the catch-up repairs a removed instrument",
        A,
        "if feed is not None and instrument in feed.instruments:",
        "if feed is not None:",
    ),
    one(
        "the catch-up repairs without the lock",
        A,
        "async with self.server.instrument_locks.hold(\n"
        '                instrument, "repair before the alert catch-up"\n'
        "            ):",
        "async with contextlib.nullcontext():",
    ),
    # What goes, and what stays
    one(
        "the engine keeps deleted alerts",
        V,
        "            self.alerts.instrument_deleted(instrument, purged.drawings, purged.alerts)\n",
        "",
    ),
    one(
        "every drawing deleted",
        P,
        'f"DELETE FROM drawings WHERE id IN ({marks})"',
        'f"DELETE FROM drawings WHERE 1 OR id IN ({marks})"',
    ),
    one(
        "every alert log deleted",
        P,
        'f"DELETE FROM alert_events WHERE alert_id IN ({marks})"',
        'f"DELETE FROM alert_events WHERE 1 OR alert_id IN ({marks})"',
    ),
    one("drawings always deleted", P, "    if drawings_and_alerts:\n", "    if True:\n"),
    one(
        "tick files kept",
        P,
        "            await asyncio.to_thread(shutil.rmtree, ticks)\n",
        "            pass\n",
    ),
    one(
        "the whole tick folder deleted",
        P,
        "ticks = ticks_dir / source / instrument",
        "ticks = ticks_dir / source",
    ),
    # VACUUM
    one(
        "the server does not compact",
        V,
        "drawings_and_alerts=drawings_and_alerts,\n            )",
        "drawings_and_alerts=drawings_and_alerts,\n                vacuum=False,\n            )",
    ),
    one("--no-vacuum ignored", M, "vacuum=not args.no_vacuum,", "vacuum=True,"),
    one("the command never compacts", M, "vacuum=not args.no_vacuum,", "vacuum=False,"),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
