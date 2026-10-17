"""Mutation check of the bar cache (docs/decisions-bar-cache.md): each mutant breaks one
rule in a copy of `src/`, and the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_bar_cache.py                    # every mutant
    python tools/mutate_bar_cache.py "no margin"        # only the ones named

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = ["tests/test_bar_cache.py"]
S = "openchart/store/candles.py"
B = "openchart/market/bars.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # The store's version counter
    one(
        "no bump before the write",
        S,
        "        self._bump(source, instrument, oldest)\n        try:",
        "        try:",
    ),
    one(
        "no bump after the write",
        S,
        "        finally:\n            self._bump(source, instrument, oldest)",
        "        finally:\n            pass",
    ),
    one(
        "listeners not told",
        S,
        "        for listener in self._listeners:\n",
        "        for listener in []:\n",
    ),
    one(
        "upsert tells the newest time, not the oldest",
        S,
        "async with self._writing(source, instrument, min(stamps)):",
        "async with self._writing(source, instrument, max(stamps)):",
    ),
    one(
        "purge tells a time instead of everything",
        S,
        "async with self._writing(source, instrument, None):",
        "async with self._writing(source, instrument, 2**62):",
    ),
    # The cut
    # Equivalent: a write changes no bar before the oldest time it wrote (the H1/M1 boundary
    # it can move lies at or after that time), so the hour of margin is a safety margin
    # only; docs/decisions-bar-cache.md. The next mutant breaks the cut without it.
    one("no margin for the H1/M1 boundary", B, "MARGIN = 3600", "MARGIN = 0", equivalent=True),
    (
        "the cut keeps the bar holding the write (no margin)",
        B,
        [
            ("MARGIN = 3600", "MARGIN = 0"),
            (
                "cut = bar_start(oldest - MARGIN, self._tfs[key])",
                "cut = bar_start(oldest - MARGIN, self._tfs[key]) + self._tfs[key].seconds",
            ),
        ],
        False,
    ),
    one(
        "everything everywhere is kept on a purge",
        B,
        "            if oldest is None:\n                self._drop(key)\n                continue",
        "            if oldest is None:\n                continue",
    ),
    one(
        "every write drops every run",
        B,
        "            if cut <= run.lo:",
        "            if True:",
    ),
    one(
        "survivors not restamped",
        B,
        "                run = _Run(run.lo, run.hi, epoch, run.cols)\n",
        "                pass\n",
    ),
    one(
        "the cut run keeps its old end",
        B,
        "run = _Run(run.lo, cut, epoch,",
        "run = _Run(run.lo, run.hi, epoch,",
    ),
    # Reads
    one(
        "a run with another epoch is used",
        B,
        "if run is not None and run.epoch != epoch:",
        "if False:",
    ),
    one(
        "a read during a write is kept",
        B,
        "        if self.store.epoch(source, instrument) != epoch:\n",
        "        if False:\n",
    ),
    one(
        "a hit when the request starts before the run",
        B,
        "if run is not None and run.lo <= lo and hi <= run.hi:",
        "if run is not None and run.lo <= lo + 3600 and hi <= run.hi:",
    ),
    one(
        "a hit when the request ends after the run",
        B,
        "if run is not None and run.lo <= lo and hi <= run.hi:",
        "if run is not None and run.lo <= lo and hi <= run.hi + 300:",
    ),
    one(
        "a join reads only what is missing before the run",
        B,
        "((lo, run.lo), (run.hi, hi))",
        "((lo, run.lo),)",
    ),
    one(
        "a join reads only what is missing after the run",
        B,
        "((lo, run.lo), (run.hi, hi))",
        "((run.hi, hi),)",
    ),
    one(
        "a trimmed run keeps its old start",
        B,
        'run = _Run(int(cols["ts"][0]), run.hi, run.epoch, cols)',
        "run = _Run(run.lo, run.hi, run.epoch, cols)",
    ),
    one(
        "no size cap", B, "        while self.nbytes > self.max_bytes:\n", "        while False:\n"
    ),
    one(
        "4h, D and W cached too",
        B,
        "return tf.kind is Kind.FIXED",
        "return tf.kind is not Kind.TICK",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
