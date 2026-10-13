"""Finding the indicator files at startup and turning each into an `Indicator`."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import openchart.indicators as package
from openchart.indicators._base import Indicator, Output, Param

log = logging.getLogger(__name__)

_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass
class Registry:
    indicators: dict[str, Indicator] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)  # {file, error}

    def get(self, indicator_id: str) -> Indicator | None:
        return self.indicators.get(indicator_id)

    def catalog(self) -> list[Indicator]:
        return sorted(self.indicators.values(), key=lambda i: i.title.lower())


def load_indicators(user_dir: Path | None = None) -> Registry:
    """The built-in indicators, then the owner's files in `user_dir` (which win on a
    clash of names). A file that fails to load is reported, not fatal."""
    registry = Registry()
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        _add(
            registry,
            info.name,
            f"openchart/indicators/{info.name}.py",
            builtin=True,
            load=lambda name=info.name: importlib.import_module(f"{package.__name__}.{name}"),
        )
    if user_dir is not None and user_dir.is_dir():
        for path in sorted(user_dir.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            _add(registry, path.stem, str(path), builtin=False, load=lambda p=path: _import_file(p))
    return registry


def _add(registry: Registry, stem: str, where: str, *, builtin: bool, load) -> None:
    try:
        if not _ID.match(stem):
            raise ValueError("the file name must be lowercase letters, digits and _")
        indicator = from_module(stem, load(), builtin=builtin)
    except Exception as exc:  # anything a broken file can raise
        log.error("indicator %s not loaded: %s", where, exc)
        registry.errors.append({"file": where, "error": f"{type(exc).__name__}: {exc}"})
        return
    if stem in registry.indicators:
        log.warning("indicator %s replaces the built-in one", where)
    registry.indicators[stem] = indicator


def _import_file(path: Path) -> ModuleType:
    name = f"openchart_user_indicators.{path.stem}"
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


def from_module(stem: str, module: ModuleType, *, builtin: bool = True) -> Indicator:
    """Check what a file declares (see `_base.py`)."""

    def need(attr: str, kind: type | tuple[type, ...]):
        if not hasattr(module, attr):
            raise ValueError(f"missing {attr}")
        value = getattr(module, attr)
        if not isinstance(value, kind):
            raise ValueError(f"{attr} has the wrong type")
        return value

    name = need("NAME", str).strip()
    title = getattr(module, "TITLE", name)
    overlay = need("OVERLAY", bool)
    params = tuple(need("PARAMS", list | tuple))
    outputs = tuple(need("OUTPUTS", list | tuple))
    compute = need("compute", object)
    if not callable(compute):
        raise ValueError("compute must be a function")
    if not name or not isinstance(title, str):
        raise ValueError("NAME and TITLE must be text")
    if not all(isinstance(p, Param) for p in params):
        raise ValueError("PARAMS must hold Int(...), Source(...), Color(...) and the like")
    if not outputs or not all(isinstance(o, Output) for o in outputs):
        raise ValueError("OUTPUTS must hold at least one Output(...)")
    names = [p.name for p in params]
    if len(set(names)) != len(names):
        raise ValueError("two parameters have the same name")
    by_name = {p.name: p for p in params}
    for o in outputs:
        for role, ref in (("color", o.color), ("width", o.width)):
            if by_name.get(ref) is None or by_name[ref].type != role:
                raise ValueError(f"output {o.name!r}: {ref!r} is not a {role} parameter")
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
            raise ValueError("warmup must be a function")
        indicator.warmup = warmup
    indicator.normalize(None)  # the defaults themselves must be valid
    return indicator
