"""Write THIRD-PARTY-LICENSES.txt for a build, and check the licences against Apache 2.0.

Run it with the Python environment the build bundles (PyInstaller copies those packages
into the program), from the repository root:

    python packaging/third_party_licenses.py --root openchart --out server/THIRD-PARTY-LICENSES.txt
    python packaging/third_party_licenses.py --root pywebview --no-frontend --out viewer/...txt

(packaging/build-windows.ps1 runs both.)

It follows the requirements of `--root` (with the extras actually installed), adds the
frontend's runtime packages (bundled into the web page) and Python itself, copies each
licence text, and exits with status 1 if a licence is not one known to be compatible
with distributing the result under Apache 2.0 (see ALLOWED), or is unknown.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

# Licences whose terms allow bundling in an Apache-2.0 program (permissive, or MPL-2.0's
# file-level copyleft for unmodified files). Matched against the declared licence text.
ALLOWED = [
    r"\bApache\b",
    r"\bMIT\b",
    r"\bBSD\b",
    r"\bISC\b",
    r"\bPSF\b|Python Software Foundation",
    r"MPL[- ]?2\.0|Mozilla Public License 2\.0",
    r"\bUnlicense\b|Public Domain|CC0",
    r"\bZlib\b",
    r"HPND|Historical Permission Notice",
]
DENIED = [r"\bA?GPL\b|General Public License", r"\bSSPL\b", r"Commons Clause", r"\bBUSL\b"]
# GPL with a linking exception, as declared by these packages, is fine for the bundle.
EXCEPTIONS = {
    "pyinstaller": "GPL-2.0 with the bootloader exception: the built program is not covered",
    "pyinstaller-hooks-contrib": "GPL-2.0 / Apache-2.0 (build tool, hooks run at build time only)",
}
SKIP = {"pip", "setuptools", "wheel", "pip-licenses", "packaging", "openchart"}
FRONTEND_RUNTIME = [
    "lightweight-charts",
    "fancy-canvas",
    "lucide",
    # The code editor (CodeMirror 6 with its Python mode), loaded when the editor first opens.
    "codemirror",
    "@codemirror/autocomplete",
    "@codemirror/commands",
    "@codemirror/lang-python",
    "@codemirror/language",
    "@codemirror/lint",
    "@codemirror/search",
    "@codemirror/state",
    "@codemirror/streamparser",
    "@codemirror/view",
    "@lezer/common",
    "@lezer/highlight",
    "@lezer/lr",
    "@lezer/python",
    "@marijn/find-cluster-break",
    "crelt",
    "style-mod",
    "w3c-keyname",
]
LICENSE_NAMES = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(root: str) -> list[md.Distribution]:
    """`root` and everything it requires (installed extras included)."""
    seen: dict[str, md.Distribution] = {}
    todo = [(root, set())]
    while todo:
        name, extras = todo.pop()
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        key = norm(dist.metadata["Name"])
        if key in seen and not extras:
            continue
        seen[key] = dist
        for spec in dist.requires or []:
            req = Requirement(spec)
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if req.marker and not any(req.marker.evaluate(env) for env in envs):
                continue
            todo.append((req.name, set(req.extras)))
    return sorted(seen.values(), key=lambda d: norm(d.metadata["Name"]))


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    declared = (meta.get("License") or "").strip()
    if classifiers:
        return "; ".join(classifiers)
    if declared and len(declared) < 200:
        return declared
    return declared[:200] + "..." if declared else "UNKNOWN"


def licence_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        if LICENSE_NAMES.match(Path(str(f)).name) or "licenses/" in str(f).replace("\\", "/"):
            path = Path(dist.locate_file(f))
            if path.is_file() and path.stat().st_size < 400_000:
                out.append((str(f), path.read_text(encoding="utf-8", errors="replace")))
    if not out:
        declared = dist.metadata.get("License") or ""
        if len(declared) > 200:  # some packages put the whole text there
            out.append(("License (metadata)", declared))
    return out


def verdict(name: str, licence: str) -> str | None:
    """None if fine, else the problem."""
    if norm(name) in EXCEPTIONS:
        return None
    if any(re.search(p, licence, re.I) for p in DENIED) and not re.search(
        r"\bLGPL\b.*(exception|linking)|with.*exception", licence, re.I
    ):
        return f"copyleft licence: {licence}"
    if not any(re.search(p, licence, re.I) for p in ALLOWED):
        return f"unknown or unreviewed licence: {licence}"
    return None


def frontend(repo: Path) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    out = []
    for name in FRONTEND_RUNTIME:
        folder = repo / "frontend" / "node_modules" / name
        pkg = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        texts = [
            (p.name, p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(folder.iterdir())
            if p.is_file() and LICENSE_NAMES.match(p.name)
        ]
        out.append((name, pkg.get("version", "?"), pkg.get("license", "UNKNOWN"), texts))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", required=True, help="the distribution whose dependencies are bundled"
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-frontend", action="store_true", help="the build has no web page")
    parser.add_argument("--extra", action="append", default=[], help="more distributions bundled")
    parser.add_argument(
        "--add",
        action="append",
        default=[],
        help="NAME|VERSION|LICENCE|FILE of other bundled files",
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]

    entries = []  # (name, version, licence, texts)
    dists = closure(args.root)
    for extra in args.extra:
        dists += [d for d in closure(extra) if d not in dists]
    for dist in dists:
        name = dist.metadata["Name"]
        if norm(name) in SKIP:
            continue
        licence, texts = licence_of(dist), licence_texts(dist)
        if licence == "UNKNOWN" and texts:  # declared only in its licence file
            first = next((ln.strip() for ln in texts[0][1].splitlines() if ln.strip()), "")
            licence = f"{first} (from its licence file)"
        entries.append((name, dist.version, licence, texts))
    for spec in args.add:
        # Files bundled outside Python packages: NAME|VERSION|LICENCE|LICENCE-FILE
        name, version, licence, text_file = spec.split("|", 3)
        text = Path(text_file).read_text(encoding="utf-8", errors="replace")
        entries.append((name, version, licence, [(Path(text_file).name, text)]))
    if not args.no_frontend:
        entries += [(f"{n} (web page, npm)", v, lic, t) for n, v, lic, t in frontend(repo)]
    python_licence = Path(sys.base_prefix) / "LICENSE.txt"
    entries.append(
        (
            "Python",
            sys.version.split()[0],
            "PSF-2.0",
            [("LICENSE.txt", python_licence.read_text(encoding="utf-8", errors="replace"))]
            if python_licence.is_file()
            else [],
        )
    )

    problems = []
    lines = [
        "Third-party software bundled with this OpenChart build",
        "=" * 56,
        "",
        "OpenChart is licensed under the Apache License 2.0 (LICENSE); see NOTICE for the",
        "attributions. This build also contains the following software, under the licences",
        "below.",
        "",
        "Summary",
        "-------",
    ]
    for name, version, licence, _ in entries:
        lines.append(f"  {name} {version}: {licence}")
        problem = verdict(name.split(" (")[0], licence)
        if problem:
            problems.append(f"{name} {version}: {problem}")
    for name, version, licence, texts in entries:
        lines += ["", "", "=" * 78, f"{name} {version}", f"Licence: {licence}", "=" * 78]
        if norm(name) in EXCEPTIONS:
            lines.append(f"Note: {EXCEPTIONS[norm(name)]}")
        for title, text in texts:
            lines += ["", f"--- {title} ---", "", text.rstrip()]
        if not texts:
            lines += ["", "(No licence file shipped with the package; licence as declared above.)"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(entries)} components, written to {out}")
    for p in problems:
        print(f"CHECK: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
