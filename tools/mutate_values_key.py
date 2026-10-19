"""Mutation check of the values cache key and the warmup notice (#47 on top of #46 and
#49): each mutant breaks the key, the notice or the store's version counter in a copy of
`src/`, and the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_values_key.py                   # every mutant
    python tools/mutate_values_key.py "key without the inputs"

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_warmup_notice_cache.py",
    "tests/test_warmup_notice.py",
    "tests/test_values_cache.py",
]
R = "openchart/api/routes_indicators.py"
S = "openchart/store/candles.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # The values cache key: everything the values and the warmup cut depend on
    # Equivalent: the version that follows is "builtin" for a built-in and the file's hash
    # for a custom one, so it already tells the two kinds apart.
    one(
        "key without the kind",
        R,
        '        "custom" if custom is not None else "builtin",\n',
        "",
        equivalent=True,
    ),
    one(
        "key without the indicator id",
        R,
        "        indicator.id,\n        custom.digest",
        "        custom.digest",
    ),
    one(
        "key without the inputs",
        R,
        "json.dumps({k: v for k, v in settings.items() if k not in style}, sort_keys=True)",
        '"{}"',
    ),
    one(
        "key without the price type (info)",
        R,
        "        json.dumps(info.to_json(), sort_keys=True),\n",
        "",
    ),
    one(
        "key without the bars",
        R,
        "return (*about, frame_digest(frame)), window",
        "return about, window",
    ),
    one(
        "key without the bars or their count",
        R,
        "return (*about, frame_digest(frame)), window",
        "return about[:-1], window",
    ),
    # The notice
    one(
        "notice from the cut warmup",
        R,
        "        needed = warm\n        warm = min(max(warm, 0), MAX_WARMUP)\n",
        "        warm = min(max(warm, 0), MAX_WARMUP)\n        needed = warm\n",
    ),
    one(
        "notice never given",
        R,
        '        if cut:\n            out["notice"] = cut',
        '        if False:\n            out["notice"] = cut',
    ),
    one(
        "notice at the limit too", R, "    if needed <= MAX_WARMUP:", "    if needed < MAX_WARMUP:"
    ),
    one(
        "notice written into the stored checks",
        R,
        'out["checks"] = {**(out["checks"] or {}), "warmup_cut": cut}',
        'out["checks"]["warmup_cut"] = cut',
    ),
    # The store's version counter, as the values cache sees it through the bar cache
    one(
        "the version counter never moves",
        S,
        "        self._epoch[key] += 1\n        for listener in self._listeners:\n",
        "        return\n        for listener in self._listeners:\n",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
