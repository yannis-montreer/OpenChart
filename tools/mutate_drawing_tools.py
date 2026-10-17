"""Mutation check of the new drawing tools (docs/decisions-drawing-tools.md): each mutant
breaks one rule, and the tests must fail on it. Two kinds:

- server mutants break a copy of `src/` (same runner as `mutate_checks.py`) and run
  `tests/test_drawing_tools.py` and `tests/test_drawings.py`;
- browser mutants break `frontend/src/drawings/shapes.ts` in place (restored in a
  `finally`, and the file's text is checked afterwards) and run vitest on the drawing
  tests. Needs Node 24 on PATH and `npm ci` done in `frontend/`.

    python tools/mutate_drawing_tools.py            # every mutant
    python tools/mutate_drawing_tools.py server     # only the server's
    python tools/mutate_drawing_tools.py browser    # only the browser's

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2 when
one no longer applies or the tests fail on the unbroken code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import mutate_checks as runner

ROOT = Path(__file__).resolve().parents[1]
RULES = "openchart/drawing_rules.py"
DR = "openchart/api/routes_drawings.py"
AL = "openchart/api/routes_alerts.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


SERVER = [
    one("rect on one price", RULES, 'if points[0]["p"] == points[1]["p"]:', "if False:"),
    one(
        "rect and fib on one time",
        RULES,
        'if no_time(points[0], points[1]):\n            return "the two corners',
        'if False:\n            return "the two corners',
    ),
    one(
        "measure needs only one difference",
        RULES,
        'if no_time(points[0], points[1]) and points[0]["p"] == points[1]["p"]:',
        'if no_time(points[0], points[1]) or points[0]["p"] == points[1]["p"]:',
    ),
    one(
        "channel base on one time",
        RULES,
        'if no_time(points[0], points[1]):\n            return "the base line',
        'if False:\n            return "the base line',
    ),
    one(
        "channel third point on the line",
        RULES,
        "if abs(channel_offset(points, clock)) < 1e-9:",
        "if abs(channel_offset(points, clock)) < -1:",
    ),
    one(
        "channel offset in clock time",
        RULES,
        'span = clock(b["t"]) - clock(a["t"])',
        'span = b["t"] - a["t"]',
    ),
    one(
        "channel offset from the third point's clock time",
        RULES,
        'clock(c["t"]) - clock(a["t"])) / span',
        '(c["t"] - a["t"])) / span',
    ),
    one(
        "text without text",
        RULES,
        'elif type_ == "text" and not (label or "").strip():',
        "elif False:",
    ),
    one("point count: at least", RULES, "if len(points) != need:", "if len(points) < need:"),
    one("vline needs two points", RULES, '"vline": 1,', '"vline": 2,'),
    one("channel needs two points", RULES, '"channel": 3,', '"channel": 2,'),
    one(
        "a rectangle is a line",
        RULES,
        'LINE_TYPES = ("hline", "trendline", "ray", "extended")',
        'LINE_TYPES = ("hline", "trendline", "ray", "extended", "rect")',
    ),
    one("a line alert on any drawing", AL, "or drawing.type not in LINE_TYPES", "or False"),
    one(
        "text not checked on update",
        DR,
        'text_changed = current.type == "text" and (body.clear_label or body.label is not None)',
        "text_changed = False",
    ),
    one(
        "update keeps the old label in the check",
        DR,
        "label = current.label if body.label is None else body.label",
        "label = current.label",
    ),
    one(
        "create ignores the label",
        DR,
        "trading_clock(row.hours, closures), body.label)\n        drawing = await",
        'trading_clock(row.hours, closures), "x")\n        drawing = await',
    ),
    one("fill above 100", DR, "Field(None, ge=0, le=100)", "Field(None, ge=0, le=1000)"),
    one("fill below 0", DR, "Field(None, ge=0, le=100)", "Field(None, ge=-5, le=100)"),
    one("extend loosely typed", DR, "extend: StrictBool | None", "extend: bool | None"),
    one("fill loosely typed", DR, "fill: StrictInt | None", "fill: int | None"),
]

SHAPES = "frontend/src/drawings/shapes.ts"
BROWSER = [
    one(
        "fib levels from the start",
        SHAPES,
        "price: b.p + (a.p - b.p) * level",
        "price: a.p + (b.p - a.p) * level",
    ),
    one(
        "a fib level missing",
        SHAPES,
        "[0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]",
        "[0, 0.236, 0.382, 0.5, 0.786, 1]",
    ),
    one(
        "channel offset reversed", SHAPES, "return pts[2].p - extendedPrice", "return extendedPrice"
    ),
    one(
        "channel base in clock time",
        SHAPES,
        "(clock(t) - clock(a.t))) / (clock(b.t) - clock(a.t))",
        "(t - a.t)) / (b.t - a.t)",
    ),
    one("channel second line below", SHAPES, "p: q.p + d }", "p: q.p - d }"),
    one("channel never rays", SHAPES, 'extend ? "ray" : "trendline"', '"trendline"'),
    one(
        "channel base drag loses the offset",
        SHAPES,
        "extendedPrice(base[0], base[1], t3, clock) + d }",
        "extendedPrice(base[0], base[1], t3, clock) }",
    ),
    one(
        "rect corner 1 takes the wrong anchor",
        SHAPES,
        "[{ t: a.t, p: to.p }, { t: to.t, p: b.p }],",
        "[{ t: to.t, p: a.p }, { t: a.t, p: to.p }],",
    ),
    one(
        "rect corner 3 takes the wrong anchor",
        SHAPES,
        "[{ t: to.t, p: a.p }, { t: b.t, p: to.p }],",
        "[{ t: a.t, p: to.p }, { t: to.t, p: b.p }],",
    ),
    one(
        "a drag may break the rules",
        SHAPES,
        "return geometryError(type, next, clock) ? null : next;",
        "return next;",
    ),
    one(
        "vline moves in price",
        SHAPES,
        'p: type === "vline" ? q.p : round(q.p + dPrice)',
        "p: round(q.p + dPrice)",
    ),
    one("hline moves in time", SHAPES, 'type === "hline" ? q.t :', "false ? q.t :"),
    one(
        "vline handle moves in price", SHAPES, "next = [{ t: to.t, p: pts[0].p }];", "next = [to];"
    ),
    one("hline handle moves in time", SHAPES, "next = [{ t: pts[0].t, p: to.p }];", "next = [to];"),
    one(
        "measure percent of the end",
        SHAPES,
        "percent: (change / a.p) * 100",
        "percent: (change / b.p) * 100",
    ),
    one(
        "measure bars reversed",
        SHAPES,
        "Math.round(index.toLogical(b.t) - index.toLogical(a.t))",
        "Math.round(index.toLogical(a.t) - index.toLogical(b.t))",
    ),
    one("measure pips multiplied", SHAPES, "change / pipSize", "change * pipSize"),
    one("measure seconds reversed", SHAPES, "seconds: b.t - a.t", "seconds: a.t - b.t"),
    one(
        "duration of three units",
        SHAPES,
        "if (parts.length === 2) break;",
        "if (parts.length === 3) break;",
    ),
    one("duration keeps its sign", SHAPES, "Math.round(Math.abs(seconds))", "Math.round(seconds)"),
    one("alpha in tenths", SHAPES, "Math.max(0, percent)) / 100", "Math.max(0, percent)) / 10"),
    one("alpha not capped", SHAPES, "Math.min(100, Math.max(0, percent))", "Math.max(0, percent)"),
    one("text size 3", SHAPES, "[12, 14, 18, 24]", "[12, 14, 16, 24]"),
    one(
        "text size clamped low",
        SHAPES,
        "Math.max(1, Math.round(width))",
        "Math.max(0, Math.round(width))",
    ),
    one("text without text", SHAPES, 'if (type === "text" && !(label ?? "").trim())', "if (false)"),
    one("rect on one price", SHAPES, "return pts[0].p === pts[1].p ?", "return false ?"),
    one(
        "measure needs only one difference",
        SHAPES,
        "return SAME(pts[0], pts[1], clock) && pts[0].p === pts[1].p ?",
        "return SAME(pts[0], pts[1], clock) || pts[0].p === pts[1].p ?",
    ),
    one("channel third point on the line", SHAPES, "< 1e-9 ?", "< -1 ?"),
    one(
        "a line has no time between its points",
        SHAPES,
        'return SAME(pts[0], pts[1], clock) ? "the two points need trading time',
        'return false ? "the two points need trading time',
    ),
    one(
        "rect box not normalised",
        SHAPES,
        "t0: Math.min(a.t, b.t), t1: Math.max(a.t, b.t)",
        "t0: a.t, t1: b.t",
    ),
    one(
        "rect corners in another order",
        SHAPES,
        "return [a, { t: b.t, p: a.p }, b, { t: a.t, p: b.p }];",
        "return [a, { t: a.t, p: b.p }, b, { t: b.t, p: a.p }];",
    ),
    one("rect fill default", SHAPES, 'type === "rect" ? 15 :', 'type === "rect" ? 10 :'),
    one(
        "channel fill default", SHAPES, 'type === "channel" ? 10 : 0', 'type === "channel" ? 15 : 0'
    ),
    one("stored fill ignored", SHAPES, "if (fill !== undefined) return fill;", ""),
    one(
        "fill offered to a rectangle only",
        SHAPES,
        'fill: type === "rect" || type === "channel",',
        'fill: type === "rect",',
    ),
    one(
        "extend offered to a rectangle",
        SHAPES,
        'extend: type === "channel" || type === "fib",',
        'extend: type === "channel" || type === "fib" || type === "rect",',
    ),
    one(
        "label offered to a measure",
        SHAPES,
        'label: type !== "fib" && type !== "measure",',
        'label: type !== "fib",',
    ),
    one("dash offered to a text", SHAPES, 'dash: type !== "text",', "dash: true,"),
    one(
        "a rectangle is a line",
        SHAPES,
        'const LINE_TYPE_LIST: readonly DrawingType[] = ["hline", "trendline", "ray", "extended"];',
        "const LINE_TYPE_LIST: readonly DrawingType[] = "
        '["hline", "trendline", "ray", "extended", "rect"];',
    ),
    one("contrast threshold", SHAPES, "lum > 0.6", "lum > 0.99"),
]


def vitest() -> subprocess.CompletedProcess[str]:
    npx = shutil.which("npx") or "npx"
    return subprocess.run(
        [npx, "vitest", "run", "src/drawings"],
        cwd=ROOT / "frontend",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        shell=sys.platform == "win32",
    )


def browser() -> int:
    base = vitest()
    print(
        f"baseline (browser): {(base.stdout.strip().splitlines() or ['no output'])[-1]}", flush=True
    )
    if base.returncode:
        print("the browser tests fail on the unbroken code: no mutation run")
        return 2
    path = ROOT / SHAPES
    original = path.read_bytes()  # bytes: the file keeps its line endings
    killed, survived, stale = [], [], []
    try:
        for name, _, edits, equivalent in BROWSER:
            text = original.decode("utf-8")
            for old, new in edits:
                if text.count(old) != 1:
                    break
                text = text.replace(old, new)
            else:
                path.write_bytes(text.encode("utf-8"))
                got = vitest()
                path.write_bytes(original)
                if got.returncode:
                    killed.append(name)
                    failed = [ln.strip() for ln in got.stdout.splitlines() if "FAIL" in ln]
                    print(f"{name}: killed by {failed[0] if failed else 'an error'}", flush=True)
                else:
                    verdict = "SURVIVED" + (" (equivalent, expected)" if equivalent else "")
                    if not equivalent:
                        survived.append(name)
                    print(f"{name}: {verdict}", flush=True)
                continue
            stale.append(name)
            print(f"{name}: NOT APPLIED (the code changed)", flush=True)
    finally:
        path.write_bytes(original)
    print(
        f"\nbrowser: {len(killed)} killed, {len(survived)} survived, "
        f"{len(stale)} not applied, of {len(BROWSER)}"
    )
    return 2 if stale else 1 if survived else 0


def server() -> int:
    runner.TESTS = ["tests/test_drawing_tools.py", "tests/test_drawings.py"]
    runner.MUTANTS = SERVER
    return runner.main([])


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    codes = []
    if which in ("all", "server"):
        codes.append(server())
    if which in ("all", "browser"):
        codes.append(browser())
    sys.exit(max(codes))
