"""The licence list of the web page's runtime packages matches what the page bundles.

`packaging/third_party_licenses.py` lists the npm packages whose licences go into
THIRD-PARTY-LICENSES.txt by name. A package added to `frontend/package.json` without being
added there would ship without its licence text: this compares the list with the lockfile's
non-dev packages.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _runtime_list() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "third_party_licenses", ROOT / "packaging" / "third_party_licenses.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.FRONTEND_RUNTIME)


def _locked_runtime() -> set[str]:
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    names = set()
    for path, info in lock["packages"].items():
        if not path or info.get("dev"):
            continue
        # "node_modules/@codemirror/state" -> "@codemirror/state"; nested copies keep their own name
        names.add(path.rsplit("node_modules/", 1)[1])
    return names


def test_every_runtime_package_of_the_page_has_its_licence_listed():
    locked = _locked_runtime()
    listed = _runtime_list()
    assert locked - listed == set(), "add these to FRONTEND_RUNTIME (packaging/third_party_licenses.py)"
    assert listed - locked == set(), "these are listed but no longer a runtime dependency"
