"""Mutation check of CI-4's write endpoints and file rules: each mutant breaks one rule in
a copy of `src/`, and the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_definitions.py                 # every mutant
    python tools/mutate_definitions.py "no size limit"  # only the ones named

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = [
    "tests/test_definitions_api.py",
    "tests/test_request_ids.py",
    "tests/test_security_review.py",
]
F = "openchart/indicators/_files.py"
D = "openchart/api/routes_definitions.py"
RU = "openchart/indicators/worker/runner.py"
CH = "openchart/indicators/worker/child.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


GATE = "        await require_editing(request, server.settings, server.isolation)\n"

MUTANTS = [
    # Ids and names
    one("device names are ids", F, " and text not in DEVICES", ""),
    one("ids by match, not fullmatch", F, "bool(ID.fullmatch(text))", "bool(ID.match(text))"),
    one(
        "import: the whole path, not the base name",
        F,
        'name = re.split(r"[\\\\/]", filename)[-1]',
        "name = filename",
    ),
    one("import: accents kept", F, "if not unicodedata.combining(ch)", "if True"),
    one(
        "import: device names kept",
        F,
        'return name + "_" if name in DEVICES else name',
        "return name",
    ),
    # Sources
    one("BOM kept", F, 'if text.startswith("\\ufeff"):\n        text = text[1:]\n', ""),
    one("NUL allowed", F, 'if "\\x00" in text:', "if False:"),
    one("no size limit", F, "if size > MAX_FILE_BYTES:", "if False:"),
    one("size in characters", F, 'size = len(text.encode("utf-8"))', "size = len(text)"),
    # Plain files and folders
    one(
        "junctions are not links",
        F,
        ' or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)',
        "",
    ),
    one("hard links accepted", F, " and st.st_nlink <= 1", ""),
    one(
        "any name taken as the definition",
        F,
        "        if not _plain_file(path):\n",
        "        if False:\n",
    ),
    one(
        "history: links followed",
        F,
        "            if _is_link(p) or not p.is_dir():",
        "            if not p.is_dir():",
    ),
    # Equivalent: a link anywhere on the way is refused by the line above first.
    one(
        "history: outside allowed",
        F,
        '        if mine.resolve().parent != root / ".history":',
        "        if False:",
        True,
    ),
    (
        "New overwrites",
        F,
        [
            (
                "        if os.path.lexists(path):\n            raise FileProblem(409, {",
                "        if False:\n            raise FileProblem(409, {",
            ),
            (
                "                os.rename(tmp, path)  # fails if",
                "                os.replace(tmp, path)  # fails if",
            ),
            (
                "                os.link(tmp, path)  # the same",
                "                os.replace(tmp, path)  # the same",
            ),
        ],
        False,
    ),
    # Saves and the history
    one(
        "base_version ignored",
        F,
        "if base_version is not None and version_of(current) != base_version:",
        "if False:",
    ),
    one(
        "no history kept",
        F,
        "        kept = self._keep(folder, current)\n",
        '        kept = folder / "none.py"\n',
    ),
    one("eleven versions", F, "HISTORY_KEEP = 10", "HISTORY_KEEP = 11"),
    one(
        "a write in place",
        F,
        "            os.replace(tmp, path)\n        except OSError as exc:\n",
        "            path.write_bytes(text.encode('utf-8'))\n        except OSError as exc:\n",
    ),
    one(
        "a failed save keeps its version",
        F,
        "            kept.unlink(missing_ok=True)  # the save",
        "            pass  # the save",
    ),
    one("versions oldest first", F, "        found.sort(reverse=True)", "        found.sort()"),
    # Titles
    one("two TITLE lines accepted", F, "    if len(starts) != 1:", "    if not starts:"),
    one(
        "title not escaped",
        F,
        'f"TITLE = {json.dumps(title, ensure_ascii=False)}',
        'f"TITLE = \\"{title}\\"',
    ),
    one(
        "control characters in titles",
        F,
        'if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in title):',
        "if False:",
    ),
    one("61-character titles", F, "not 1 <= len(title) <= 60", "not 1 <= len(title) <= 61"),
    one("duplicate: no (copy)", D, "f\"{(old or '')[:53]} (copy)\"", "old or ''"),
    # The gate on each kind of route
    one(
        "templates without the gate",
        D,
        "    async def list_templates(request: Request) -> list[dict[str, str]]:\n" + GATE,
        "    async def list_templates(request: Request) -> list[dict[str, str]]:\n",
    ),
    one(
        "create without the gate",
        D,
        '        """New from a template, or Import of a source (never over an existing file)."""\n'
        + GATE,
        '        """New from a template, or Import of a source (never over an existing file)."""\n',
    ),
    one(
        "save without the gate",
        D,
        "body: SaveBody, request: Request) -> dict[str, Any]:\n" + GATE,
        "body: SaveBody, request: Request) -> dict[str, Any]:\n",
    ),
    one(
        "preview without the gate",
        D,
        '        """Run unsaved source on a chart query: values or the error, nothing stored."""\n'
        + GATE,
        '        """Run unsaved source on a chart query: values or the error, nothing stored."""\n',
    ),
    # Concurrency and preview
    one(
        "saves not serialized",
        D,
        "        async with lock(indicator_id):\n            changed = await run(",
        "        if True:\n            changed = await run(",
    ),
    one(
        "preview trusts the worker",
        D,
        "            series = indicator.check_wire(out, len(frame))",
        "            series = {k: __import__('pandas').Series(v) for k, v in out.items()}",
    ),
    # Equivalent on NTFS: the write itself changes the modification time the catalog reads.
    one(
        "written files not read again", D, "        server.custom.forget(indicator_id)\n", "", True
    ),
    # Request ids (S6)
    one(
        "request id not checked",
        RU,
        '            if got[0].get("rid") != rid:',
        "            if False:",
    ),
    one("one request id for all", RU, "        self._rid += 1\n", "        self._rid = 1\n"),
    one(
        "worker does not echo the id",
        CH,
        '            head = {**head, "rid": req["rid"]}\n',
        "            pass\n",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
