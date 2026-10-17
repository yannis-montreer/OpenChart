"""What New can start from (CI-4): `blank` and the four examples of
docs/custom-indicators.md, which are tested there (tests/test_doc_examples.py; a test
keeps these copies the same). They are text for new files, never loaded as indicators
here: the registry lists only the package's own folder, not this one."""

from __future__ import annotations

import ast
from pathlib import Path

from openchart.indicators._files import title_of

FOLDER = Path(__file__).resolve().parent
ORDER = ("blank", "sma_cross", "rsi_bands", "bollinger_fill", "engulfing")


def source(template_id: str) -> str | None:
    if template_id not in ORDER:
        return None
    return (FOLDER / f"{template_id}.py").read_text(encoding="utf-8")


def listing() -> list[dict[str, str]]:
    out = []
    for template_id in ORDER:
        text = source(template_id) or ""
        doc = ast.get_docstring(ast.parse(text)) or ""
        out.append(
            {
                "id": template_id,
                "title": title_of(text) or template_id,
                "description": " ".join(doc.split("\n\n")[0].split()),
            }
        )
    return out
