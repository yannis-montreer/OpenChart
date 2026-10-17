"""The built-in indicators: found at startup and turned each into an `Indicator`.

Built-in indicators run inside the server (decision D5): they are code we ship and test.
Your own files in `<data dir>/indicators/` are never imported here; the worker reads
them (`_custom.py`, `worker/`). The two are separate namespaces: a file of yours named
`ema.py` no longer replaces the built-in EMA. `from_module` checks what a file declares,
for both.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import openchart.indicators as package
from openchart.indicators._base import Indicator, SchemaError, validate_schema

log = logging.getLogger(__name__)

_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")  # \Z, not $: `$` also matches before a newline
BUILTIN_DIR = Path(package.__file__).resolve().parent


@dataclass
class Registry:
    indicators: dict[str, Indicator] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)  # {file, error}

    def get(self, indicator_id: str) -> Indicator | None:
        return self.indicators.get(indicator_id)

    def catalog(self) -> list[Indicator]:
        return sorted(self.indicators.values(), key=lambda i: i.title.lower())


def load_indicators() -> Registry:
    """The built-in indicators. A file that fails to load is reported, not fatal."""
    registry = Registry()
    for path in _files(BUILTIN_DIR):
        where = f"openchart/indicators/{path.name}"
        try:
            if not _ID.match(path.stem):
                raise ValueError("the file name must be lowercase letters, digits and _")
            indicator = from_module(path.stem, _import_file(path), builtin=True)
        except Exception as exc:  # anything a broken file can raise
            log.error("indicator %s not loaded: %s", where, exc)
            registry.errors.append({"file": where, "error": f"{type(exc).__name__}: {exc}"})
            continue
        registry.indicators[path.stem] = indicator
    return registry


def _files(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.glob("*.py")) if not p.stem.startswith("_")]


def _import_file(path: Path) -> ModuleType:
    """Run a built-in indicator file as a module of its own. Not as
    `openchart.indicators.<name>`: importing a submodule sets an attribute of the package,
    so `ema.py` would replace the `ema` function that indicator files import from it."""
    name = f"openchart_indicator_files.builtin.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def from_module(
    stem: str, module: ModuleType, *, builtin: bool = True, file: str | None = None
) -> Indicator:
    """Check what a file declares (`_base.validate_schema`); SchemaError names the file."""
    file = file or f"{stem}.py"

    def need(attr: str, kind: type | tuple[type, ...]):
        if not hasattr(module, attr):
            raise SchemaError(f"{file}: missing {attr}")
        value = getattr(module, attr)
        if not isinstance(value, kind):
            raise SchemaError(f"{file}: {attr} has the wrong type")
        return value

    name = need("NAME", str).strip()
    title = getattr(module, "TITLE", name)
    overlay = need("OVERLAY", bool)
    params = tuple(need("PARAMS", list | tuple))
    outputs = tuple(need("OUTPUTS", list | tuple))
    compute = need("compute", object)
    if not callable(compute):
        raise SchemaError(f"{file}: compute must be a function")
    validate_schema(file, name, title, params, outputs)
    warmup = getattr(module, "warmup", None)
    indicator = Indicator(
        id=stem,
        name=name,
        title=title,
        overlay=overlay,
        params=params,
        outputs=outputs,
        compute=compute,
        path=Path(module.__file__) if getattr(module, "__file__", None) else None,
        builtin=builtin,
    )
    if warmup is not None:
        if not callable(warmup):
            raise SchemaError(f"{file}: warmup must be a function")
        indicator.warmup = warmup
    indicator.normalize(None)  # the defaults themselves must be valid
    return indicator
