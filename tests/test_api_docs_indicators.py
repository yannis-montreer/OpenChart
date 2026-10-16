"""docs/api.md describes what the indicator routes answer: every key of the real answers, and
every query parameter, is named in its Indicators section (they drifted when CI-1 and CI-2 added
the custom indicators, the worker and the editing gate)."""

import json
import re
from pathlib import Path

DOC = (Path(__file__).resolve().parent.parent / "docs" / "api.md").read_text(encoding="utf-8")


def section() -> str:
    m = re.search(r"^## Indicators\n(.*?)^## ", DOC, re.S | re.M)
    assert m, "docs/api.md has no Indicators section"
    return m.group(1)


def named(text: str, key: str) -> bool:
    return f"`{key}`" in text or re.search(rf"[{{, ]{re.escape(key)}[,}} :]", text) is not None


def test_every_key_of_the_catalog_answer_is_documented(client):
    body = client.get("/api/indicators").json()
    text = section()
    missing = [k for k in body if not named(text, k)]
    assert not missing, f"GET /api/indicators answers {missing}, which docs/api.md does not name"


def test_the_nested_shapes_of_the_catalog_are_documented(client):
    body = client.get("/api/indicators").json()
    text = section()
    nested = {
        "isolation": body["isolation"],
        "editing": body["editing"],
        "worker": body["worker"] or {},
        "an entry of custom": (
            body["custom"]
            or [
                {
                    "id": 0,
                    "kind": 0,
                    "file": 0,
                    "status": 0,
                    "reason": 0,
                    "line": 0,
                    "definition": 0,
                    "checks": 0,
                }
            ]
        )[0],
        "an indicator": body["indicators"][0],
    }
    missing = {where: [k for k in shape if not named(text, k)] for where, shape in nested.items()}
    assert not any(missing.values()), missing


def test_every_key_of_the_values_answer_and_every_query_parameter_is_documented(client):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "params": json.dumps({}), "limit": 20}
    body = client.get("/api/indicators/ema", params=q).json()
    text = section()
    missing = [k for k in body if not named(text, k)]
    assert not missing, f"the values answer has {missing}, which docs/api.md does not name"
    route = text[text.index("GET /api/indicators/{id}") :].split("\n", 1)[0]
    for parameter in ("instrument", "timeframe", "price", "params", "kind", "from", "to", "limit"):
        assert parameter in route, f"the query parameter {parameter!r} is not in the route's row"
