"""Mutation check of the save-time checks (CI-2 step c): each mutant breaks one rule in a
copy of `src/`, and the checks' tests must fail on it.

    python tools/mutate_checks.py            # every mutant
    python tools/mutate_checks.py "K = 0"    # only the ones named

It refuses to start unless the tests pass on the unbroken copy (a test that fails anyway
would "kill" every mutant). Exit code 0 when every mutant is killed or is one of the few
marked equivalent; 1 when one survives; 2 when one no longer applies (the code changed:
update its text here). The repository itself is never modified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_lookahead_checks.py",
    "tests/test_check_scheduling.py",
    "tests/test_checks_api.py",
]
C = "openchart/indicators/_checks.py"
U = "openchart/indicators/_custom.py"
R = "openchart/indicators/worker/runner.py"
A = "openchart/api/routes_indicators.py"

LOW_CALL = 'd.digest, d.file, d.source, job.params, bars, job.info_json, priority="low"'

# (name, file, [(text, replacement), ...], equivalent)
MUTANTS: list[tuple[str, str, list[tuple[str, str]], bool]] = [
    ("K = 0", C, [("cut = min(LOOKAHEAD_BARS, n // 4)", "cut = 0")], False),
    ("K = half the bars", C, [("n // 4)\n    else:", "n // 2)\n    else:")], False),
    (
        "lookahead compared to n-K-1 only",
        C,
        [("_differs(a[:kept], b, boolean,", "_differs(a[: kept - 1], b[:-1], boolean,")],
        False,
    ),
    (
        "lookahead from bar 1",
        C,
        [
            (
                "bad = np.flatnonzero(_differs(a[:kept], b, boolean,",
                "bad = 1 + np.flatnonzero(_differs(a[1:kept], b[1:], boolean,",
            )
        ],
        False,
    ),
    (
        "lookahead reports the last differing bar",
        C,
        [("        i = int(bad[0])\n        va, vb", "        i = int(bad[-1])\n        va, vb")],
        False,
    ),
    (
        "warmup compared inside the offset run's warmup",
        C,
        [("    s, w = plan.start, plan.warmup", "    s, w = plan.start, 0")],
        False,
    ),
    (
        "NaN vs number counted as equal",
        C,
        [("return (full_gap != other_gap) | (~full_gap", "return (~full_gap")],
        False,
    ),
    ("infinity is a number", C, [("return ~np.isfinite(a)", "return np.isnan(a)")], False),
    (
        "lookahead tolerance 0",
        C,
        [("LOOKAHEAD_TOLERANCE = 1e-9", "LOOKAHEAD_TOLERANCE = 0.0")],
        False,
    ),
    (
        "lookahead tolerance 1e-3",
        C,
        [("LOOKAHEAD_TOLERANCE = 1e-9", "LOOKAHEAD_TOLERANCE = 1e-3")],
        False,
    ),
    ("warmup tolerance 0", C, [("WARMUP_TOLERANCE = 1e-7", "WARMUP_TOLERANCE = 0.0")], False),
    ("warmup tolerance 1e-3", C, [("WARMUP_TOLERANCE = 1e-7", "WARMUP_TOLERANCE = 1e-3")], False),
    (
        "scale from the shortened run",
        C,
        [("LOOKAHEAD_TOLERANCE, _scale(a)))", "LOOKAHEAD_TOLERANCE, _scale(b)))")],
        False,
    ),
    (
        "warmup scale from the compared bars only",
        C,
        [("WARMUP_TOLERANCE, _scale(whole)))", "WARMUP_TOLERANCE, _scale(a)))")],
        False,
    ),
    # Markers are 0/1: they differ by 1, more than any tolerance x a scale of at most 1.
    (
        "markers given the line tolerance",
        C,
        [("    if boolean:\n        return full != other\n", "")],
        True,
    ),
    ("lower bound off by one", C, [("needed = j + w + 1", "needed = j + w")], False),
    (
        "cum not reported as never agreeing",
        C,
        [("if first + j == plan.n - 1:", "if False:")],
        False,
    ),
    ("lookahead skip guard removed", C, [("if n >= MIN_BARS:", "if True:")], False),
    ("warmup skip guard removed", C, [("    if start < 1:", "    if False:")], False),
    (
        "offset ignores the minimum of 50",
        C,
        [("start = min(max(warmup, WARMUP_OFFSET),", "start = min(warmup,")],
        False,
    ),
    (
        "near-tie rule removed",
        C,
        [("if boolean and has_lines and lines_agree:", "if False:")],
        False,
    ),
    (
        "near-tie rule without lines to tell by",
        C,
        [("if boolean and has_lines and lines_agree:", "if boolean and lines_agree:")],
        False,
    ),
    ("lines always agree", C, [("if bad.size and not boolean:", "if False:")], False),
    ("no time budget", C, [("return seconds <= RUN_SHARE * deadline", "return True")], False),
    ("budget share 1.0", C, [("RUN_SHARE = 0.8", "RUN_SHARE = 1.0")], False),
    (
        "budget share strict",
        C,
        [("return seconds <= RUN_SHARE", "return seconds < RUN_SHARE")],
        False,
    ),
    (
        "a check timeout counts toward the quarantine",
        U,
        [
            (
                "        except ScriptFailed as exc:\n            took = runner.last_job_s\n",
                "        except ScriptFailed as exc:\n            self._failed(d, exc)\n"
                "            took = runner.last_job_s\n",
            )
        ],
        False,
    ),
    (
        "warmup run started after a crash or timeout",
        U,
        [('return None, took, why if exc.kind != "error" else None', "return None, took, None")],
        False,
    ),
    (
        "checked again on every request",
        U,
        [
            (
                "if key in d.checks or len(d.checks) >= CHECK_SETTINGS",
                "if len(d.checks) >= CHECK_SETTINGS",
            )
        ],
        False,
    ),
    ("six settings per version", U, [("CHECK_SETTINGS = 5", "CHECK_SETTINGS = 6")], False),
    (
        "checks run with the defaults",
        U,
        [(LOW_CALL, LOW_CALL.replace("job.params", "{}"))],
        False,
    ),
    ("checks run without info", U, [(LOW_CALL, LOW_CALL.replace("job.info_json", "None"))], False),
    ("checks at normal priority", U, [(LOW_CALL, LOW_CALL.replace('"low"', '"normal"'))], False),
    (
        "no pause after a worker start",
        U,
        [("runner.ready_at + self.check_stagger, ", "runner.ready_at, ")],
        False,
    ),
    (
        "no gap between checks",
        U,
        [("self._last_check + self.check_gap)", "self._last_check)")],
        False,
    ),
    (
        "checks do not wait for a worker that is down",
        U,
        [("if not runner.up:", "if False:")],
        False,
    ),
    (
        "a dead worker counts as up",
        R,
        [
            (
                'return self.state == "ready" and self._live is not None and '
                "self._live.handle.alive()",
                'return self.state == "ready"',
            )
        ],
        False,
    ),
    (
        "run time includes the worker start",
        R,
        [
            (
                "            t = self.clock()\n            try:\n                got, out",
                "            try:\n                got, out",
            ),
            (
                "            live = await self._ensure()\n",
                "            t = self.clock()\n            live = await self._ensure()\n",
            ),
        ],
        False,
    ),
    (
        "low priority ignored in the runner",
        R,
        [("            if low:\n", "            if False:\n")],
        False,
    ),
    (
        "low jobs do not yield to waiting charts",
        R,
        [("lambda: not self._busy and not self._normal_waiting", "lambda: not self._busy")],
        False,
    ),
    (
        "ready_at not set on a respawn",
        R,
        [
            (
                "        self._spawning = None\n        self.ready_at = self.clock()\n",
                "        self._spawning = None\n",
            )
        ],
        False,
    ),
    (
        "built-ins checked",
        A,
        [
            (
                "        if custom is not None:  # the save-time checks",
                "        if True:  # the save-time checks",
            )
        ],
        False,
    ),
    (
        "checks not in the values answer",
        A,
        [
            (
                '            out["checks"] = server.custom.checks_for(custom, settings)',
                "            pass",
            )
        ],
        False,
    ),
    (
        "the declared warmup not passed",
        A,
        [("settings, info, warmup=warm)", "settings, info)")],
        False,
    ),
]


def pytest(copy: Path, *, first_failure: bool) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS]
    if first_failure:
        argv.insert(4, "-x")
    env = dict(os.environ, PYTHONPATH=str(copy))
    return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)


def main(names: list[str]) -> int:
    chosen = [m for m in MUTANTS if not names or m[0] in names]
    unknown = set(names) - {m[0] for m in MUTANTS}
    if unknown:
        print(f"no mutant named {sorted(unknown)}")
        return 2
    with tempfile.TemporaryDirectory(prefix="openchart-mutants-") as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(ROOT / "src", copy)
        base = pytest(copy, first_failure=False)
        summary = (base.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(f"baseline: {summary}", flush=True)
        if base.returncode:
            print("the tests fail on the unbroken code: no mutation run")
            return 2
        killed, survived, stale = [], [], []
        for name, rel, edits, equivalent in chosen:
            path = copy / rel
            original = path.read_text(encoding="utf-8")
            text = original
            for old, new in edits:
                if text.count(old) != 1:
                    break
                text = text.replace(old, new)
            else:
                path.write_text(text, encoding="utf-8")
                try:
                    got = pytest(copy, first_failure=True)
                finally:
                    path.write_text(original, encoding="utf-8")
                failed = [
                    ln for ln in got.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))
                ]
                if got.returncode:
                    killed.append(name)
                    verdict = "killed by " + (
                        failed[0].split(" - ")[0][7:] if failed else "an error"
                    )
                else:
                    verdict = "SURVIVED" + (" (equivalent, expected)" if equivalent else "")
                    if not equivalent:
                        survived.append(name)
                print(f"{name}: {verdict}", flush=True)
                continue
            stale.append(name)
            print(f"{name}: NOT APPLIED (the code changed)", flush=True)
        equivalent = len(chosen) - len(killed) - len(survived) - len(stale)
        print(
            f"\n{len(killed)} killed, {len(survived)} survived, {equivalent} equivalent, "
            f"{len(stale)} not applied, of {len(chosen)}"
        )
        return 2 if stale else 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
