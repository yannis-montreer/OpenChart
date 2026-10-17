"""Mutation check of the hardening after the security review (docs/security-review-custom-
indicators.md, "After the review"): each mutant breaks one rule in a copy of `src/`, and the
tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_hardening.py              # every mutant
    python tools/mutate_hardening.py "auto on"    # only the ones named

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_hardening.py",
    "tests/test_editing.py",
    "tests/test_security_review.py",
    "tests/test_chart_indicators.py",
]
ED = "openchart/api/editing.py"
PR = "openchart/indicators/worker/protocol.py"
RU = "openchart/indicators/worker/runner.py"
BA = "openchart/indicators/_base.py"
CU = "openchart/indicators/_custom.py"
RG = "openchart/indicators/_registry.py"
VI = "openchart/api/routes_indicators.py"
APP = "openchart/api/app.py"
RX = "openchart/api/routes_custom_indicators.py"
RC = "openchart/api/routes_charts.py"
FROM = 'from_: int | None = Query(None, alias="from", ge=-(2**53), le=2**53),'
TO = "to: int | None = Query(None, ge=-(2**53), le=2**53),"
LIMIT = "limit: int | None = Query(None, ge=1, le=2**31),"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # S1
    one(
        "auto on",
        ED,
        '    return Editing(\n        False,\n        setting,\n        f"off by default',
        '    return Editing(\n        True,\n        setting,\n        f"off by default',
    ),
    one("on below the minimum", ED, "    if not isolation.minimum_met:", "    if False:"),
    one("off allows", ED, '    if setting == "on":', '    if setting in ("on", "off"):'),
    # S2
    one(
        "deep header not caught", PR, "    except RecursionError:", "    except ZeroDivisionError:"
    ),
    one("reader ends with EOF, not bad", RU, "end = _Bad(str(exc))", "end = _EOF"),
    one("a bad frame always counts", RU, 'return self.info.get("fresh") is True', "return True"),
    one("a bad frame never counts", RU, 'return self.info.get("fresh") is True', "return False"),
    one(
        "fresh means first job only",
        RU,
        '            fresh = live.ran <= {script[0] if script else ""}\n',
        "            fresh = not live.ran\n",
    ),
    one(
        "the worker is kept after a bad frame",
        RU,
        '                self._replace(live, f"it sent something that is not a frame ({bad})")\n',
        "",
    ),
    # S3 and the ranges
    one(
        "NaN and infinity taken as whole numbers",
        BA,
        "if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):",
        "if isinstance(value, float) and value != int(value):",
    ),
    one(
        "a huge integer overflows",
        BA,
        "            except OverflowError:  # an integer",
        "            except ZeroDivisionError:  # an integer",
    ),
    one("values: from unbounded", VI, FROM, 'from_: int | None = Query(None, alias="from"),'),
    one("values: to unbounded", VI, TO, "to: int | None = None,"),
    one("values: limit unbounded", VI, LIMIT, "limit: int | None = Query(None, ge=1),"),
    one("candles: from unbounded", APP, FROM, 'from_: int | None = Query(None, alias="from"),'),
    one("candles: to unbounded", APP, TO, "to: int | None = None,"),
    one("candles: limit unbounded", APP, LIMIT, "limit: int | None = Query(None, ge=1),"),
    # S5
    one(
        "custom id ends in $",
        CU,
        r'ID = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")',
        r'ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")',
    ),
    # The built-in registry's pattern: only files shipped in the package reach it, and no
    # name of a file there can hold a newline on Windows (the tests run here).
    one(
        "built-in id ends in $",
        RG,
        r'_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")',
        r'_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")',
        True,
    ),
    # The skipped check
    one(
        "no rerun", CU, 'if not (key in d.short and settled and n >= 2 * old["bars"]):', "if True:"
    ),
    one("rerun on any more bars", CU, 'n >= 2 * old["bars"]', 'n > old["bars"]'),
    one(
        "short not recorded",
        CU,
        "            d.short.add(_settings_key(job.params))\n",
        "            pass\n",
    ),
    # Equivalent here: a check of these settings is pending only while it is queued, and
    # a request answered meanwhile has its own fresh result, not a short one to replace.
    one(
        "rerun while pending",
        CU,
        "key in d.short and settled and n >=",
        "key in d.short and n >=",
        True,
    ),
    # The delete
    one(
        "no undo when the database step fails",
        RX,
        "            except BaseException:\n"
        "                put_back(moved, target)\n"
        "                raise\n",
        "            except BaseException:\n                raise\n",
    ),
    one(
        "no undo when the history cannot move",
        RX,
        "                put_back(moved, target)\n                what =",
        "                what =",
    ),
    one(
        "charts not put back",
        RX,
        "                    await step()\n",
        "                    pass\n",
    ),
    one(
        "a move does not wait for a reader",
        "openchart/files.py",
        "            if time.monotonic() >= end:",
        "            if True:",
    ),
    # The empty import
    one(
        "an empty import is written",
        RC,
        "            if not instances:\n",
        "            if False:\n",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
