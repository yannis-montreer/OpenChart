"""Mutation check of CI-3's rules (indicators per chart, layouts' copies, the import,
where-used and delete): each mutant breaks one rule in a copy of `src/`, and the tests
must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_chart_indicators.py            # every mutant
    python tools/mutate_chart_indicators.py "no lock"  # only the ones named

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_chart_indicators.py",
    "tests/test_layouts.py",
    "tests/test_migrations.py",
]
CI = "openchart/api/chart_indicators.py"
RC = "openchart/api/routes_charts.py"
RL = "openchart/api/routes_layouts.py"
RX = "openchart/api/routes_custom_indicators.py"
RI = "openchart/api/routes_indicators.py"
ST = "openchart/store/chart_indicators.py"
MG = "openchart/store/migrations/0008_chart_indicators.sql"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # Kinds and namespaces
    one(
        "an entry without a kind prefers the custom file",
        CI,
        'kind = "builtin" if builtin is not None else "custom"',
        'kind = "custom" if await self.server.custom.get(indicator_id) else "builtin"',
    ),
    one(
        "a built-in id answers for kind custom",
        CI,
        'if kind == "builtin":\n',
        'if kind == "builtin" or builtin is not None:\n',
    ),
    one(
        "a file that does not load is reported ok",
        CI,
        "return kind, None, custom.status",
        'return kind, None, "ok"',
    ),
    # Fitting values to the definition
    one(
        "PUT lets a refused value fall back",
        CI,
        "fit(schema, params, strict=strict)",
        "fit(schema, params, strict=False)",
    ),
    one(
        "unknown inputs are not noted",
        CI,
        "reset = [name for name in given if name not in known]",
        "reset = []",
    ),
    one(
        "unknown inputs are refused (the M7 rule)",
        CI,
        "reset = [name for name in given if name not in known]\n",
        "reset = [name for name in given if name not in known]\n"
        "    if strict and reset:\n        raise ValueError('unknown input')\n",
    ),
    one(
        "unknown inputs are kept",
        CI,
        "return out, sorted(reset)",
        "return {**given, **out}, sorted(reset)",
    ),
    one(
        "a value that falls back is not noted",
        CI,
        "                reset.append(name)\n",
        "                pass\n",
    ),
    one(
        "reads do not fit to the edited definition",
        CI,
        "params, reset = fit(schema, i.params, strict=False) if schema else (i.params, [])",
        "params, reset = (i.params, [])",
    ),
    one(
        "the write's note is not sent",
        CI,
        "reset = sorted({*reset, *(resets or {}).get(i.uid, ())})",
        "reset = sorted(reset)",
    ),
    one(
        "a repeated uid is accepted",
        CI,
        '                if strict:\n                    raise HTTPException(400, f"the uid',
        '                if False:\n                    raise HTTPException(400, f"the uid',
    ),
    one(
        "an unknown definition is skipped on PUT",
        CI,
        "                if strict:\n                    raise HTTPException(400, what)",
        "                if False:\n                    raise HTTPException(400, what)",
    ),
    one(
        "a file that does not load refuses new instances",
        CI,
        "            assert kind is not None\n",
        "            assert kind is not None\n"
        "            if schema is None:\n                raise HTTPException(400, 'not loaded')\n",
    ),
    one(
        "a file that does not load loses the values",
        CI,
        "params: dict[str, Any] = dict(e.params)",
        "params: dict[str, Any] = {} if schema is None else dict(e.params)",
    ),
    # Bounds
    one(
        "NaN and infinities are kept",
        CI,
        "if isinstance(value, float) and not math.isfinite(value):",
        "if False:",
    ),
    one(
        "no size limit on values", CI, "if len(json.dumps(params)) > MAX_PARAMS_BYTES:", "if False:"
    ),
    one("31 instances", CI, "MAX_INDICATORS = 30", "MAX_INDICATORS = 31"),
    one("text of 1001 characters", CI, "MAX_TEXT = 1000", "MAX_TEXT = 1001"),
    one("65 values", CI, "MAX_PARAMS = 64", "MAX_PARAMS = 65"),
    one(
        "uids with spaces",
        CI,
        'UID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"',
        'UID_PATTERN = r"^[A-Za-z0-9_ !-]{1,40}$"',
    ),
    one(
        "ids of any form", CI, 'ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"', 'ID_PATTERN = r"^.{1,41}$"'
    ),
    # The chart routes
    one("other sources accepted", CI, "if source != self.server.source:", "if False:"),
    one(
        "unknown instruments accepted",
        CI,
        "if await self.server.store.instrument(source, instrument) is None:",
        "if False:",
    ),
    one(
        "a stale revision is accepted",
        RC,
        "if body.revision is not None and body.revision != current:",
        "if False:",
    ),
    one(
        "the import goes onto a chart already set",
        RC,
        "if await charts.store().revision(source, instrument) != 0:",
        "if False:",
    ),
    one(
        "the import goes onto a chart set to nothing",
        RC,
        "if await charts.store().revision(source, instrument) != 0:",
        "if await charts.store().list(source, instrument):",
    ),
    one(
        "the import refuses old values",
        RC,
        "charts.checked(entries, strict=False)",
        "charts.checked(entries, strict=True)",
    ),
    one(
        "no chart_indicators_changed",
        CI,
        'self.server.hub.broadcast({"type": "chart_indicators_changed", **body})',
        "pass",
    ),
    # The store and the migration
    one(
        "the revision does not move",
        ST,
        "revision = await self.revision(source, instrument) + 1",
        "revision = await self.revision(source, instrument) + 0",
    ),
    one("the order is lost", ST, "ORDER BY position", "ORDER BY uid"),
    one(
        "charts_using counts every kind",
        ST,
        "WHERE kind=? AND indicator=? GROUP BY",
        "WHERE (kind=? OR 1) AND indicator=? GROUP BY",
    ),
    one("no kind check in the table", MG, " CHECK (kind IN ('builtin', 'custom'))", ""),
    one("no unique position", MG, ",\n    UNIQUE (source, instrument, position)", ""),
    # Layouts
    one(
        "a layout without indicators copies nothing",
        RL,
        "instances = await charts.store().list(server.source, state.instrument)",
        "instances = []",
    ),
    one(
        "a layout's list is not checked",
        RL,
        "charts.checked(state.indicators, strict=True)",
        "charts.checked(state.indicators, strict=False)",
    ),
    one("apply reports nothing skipped", RL, '"skipped": skipped + left_out', '"skipped": skipped'),
    one(
        "apply refuses old values",
        RL,
        "charts.checked(entries, strict=False)",
        "charts.checked(entries, strict=True)",
    ),
    one(
        "apply ignores ?instrument=",
        RL,
        'instrument = instrument or layout.state.get("instrument")',
        'instrument = layout.state.get("instrument") or instrument',
    ),
    # Where used, and delete
    one(
        "usage without the editing gate",
        RX,
        "async def get_usage(indicator_id: str, request: Request) -> dict[str, Any]:\n"
        "        await require_editing(request, server.settings, server.isolation)\n",
        "async def get_usage(indicator_id: str, request: Request) -> dict[str, Any]:\n",
    ),
    one(
        "delete without the editing gate",
        RX,
        "        await require_editing(request, server.settings, server.isolation)\n"
        "        definition_file(indicator_id)\n"
        "        # The charts' lock",
        "        definition_file(indicator_id)\n        # The charts' lock",
    ),
    one(
        "an id with a trailing newline",
        RX,
        "if not ID.fullmatch(indicator_id)",
        'if not re.match(r"^[a-z][a-z0-9_]{0,39}$", indicator_id)',
        sys.platform == "win32",  # equivalent there: no file name can end in a newline
    ),
    one(
        "an old entry counts as custom beside a built-in",
        RX,
        "(kind is None and server.indicators.get(indicator_id) is None)",
        "kind is None",
    ),
    one(
        "a built-in entry with the id counts",
        RX,
        'return kind == "custom" or (kind is None',
        "return True or (kind is None",
    ),
    one(
        "delete in use without confirmation", RX, 'if found["in_use"] and not confirm:', "if False:"
    ),
    one(
        "delete takes built-in instances with the id",
        RX,
        'if not (i.kind == "custom" and i.indicator == indicator_id)',
        "if not (i.indicator == indicator_id)",
    ),
    one(
        "delete leaves the layouts",
        RX,
        'layout.state["indicators"] = [e for e in entries if not uses(e, indicator_id)]',
        'layout.state["indicators"] = entries',
    ),
    one(
        "the history stays",
        RX,
        '[(history, target / "history")] if history is not None else []',
        "[]",
    ),
    one(
        "the .deleted check is skipped",
        RX,
        'deleted.is_symlink() or not deleted.is_dir() or deleted.resolve() != root / ".deleted"',
        "False",
    ),
    (
        "instances removed before the files move",
        RX,
        [
            (
                '            try:\n                if found["in_use"]:\n'
                "                    await remove_instances(indicator_id, found)\n",
                "            try:\n                pass\n",
            ),
            (
                "            try:\n                await asyncio.to_thread(move_all)\n",
                '            if found["in_use"]:\n'
                "                await remove_instances(indicator_id, found)\n"
                "            try:\n                await asyncio.to_thread(move_all)\n",
            ),
        ],
        False,
    ),
    one(
        "no indicators_changed",
        RX,
        'server.hub.broadcast({"type": "indicators_changed", "id": indicator_id, "op": "delete"})',
        "pass",
    ),
    one(
        "no layouts_changed on delete",
        RX,
        "            server.hub.broadcast(await layouts_message(server))",
        "            pass",
    ),
    # The catalog
    one(
        "errors without their kind",
        RI,
        '"error": d.reason or d.status, "kind": "custom"}',
        '"error": d.reason or d.status}',
    ),
    # Found in the review of the change (each has its test)
    (
        "a layout saved outside the lock",
        RL,
        [
            (
                "        async with charts.lock:\n            layout = await store().create(",
                "        if True:\n            layout = await store().create(",
            ),
            ("        assert charts.lock.locked()\n", ""),
        ],
        False,
    ),
    # Equivalent: the assertion only guards against a caller forgetting the lock.
    one("checked() without the lock", RL, "        assert charts.lock.locked()\n", "", True),
    one(
        "the file looked up only before the lock",
        RX,
        "            path = definition_file(indicator_id)  # again: another delete may have run\n",
        '            path = server.custom.folder / f"{indicator_id}.py"\n',
    ),
    one("lax switches", CI, "    visible: StrictBool = True\n", "    visible: bool = True\n"),
    one(
        "lax revision",
        RC,
        "revision: StrictInt | None = Field(None, ge=0)",
        "revision: int | None = None",
    ),
    one(
        "the import takes the list whole",
        RC,
        "    indicators: OldList  # checked",
        "    indicators: InstanceList  # checked",
    ),
    one(
        "no cap of 30 on an old list",
        CI,
        "            if len(instances) == MAX_INDICATORS and not strict:",
        "            if False:",
    ),
    one(
        "a value that cannot be kept fails the old list",
        CI,
        "                if strict:\n                    raise HTTPException(422, why)",
        "                if True:\n                    raise HTTPException(422, why)",
    ),
    one(
        "NaN echoed as a number",
        "openchart/api/app.py",
        "        return str(value)\n",
        "        return value\n",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
