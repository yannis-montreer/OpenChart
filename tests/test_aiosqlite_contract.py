"""`store.sqlite.atomic` runs on aiosqlite's private `Connection._execute`. These tests fail
with a message saying what to do if that method goes away or changes shape, and if the
installed version is not the pinned one (docs/decisions-indicator-alerts.md, "Upgrading
aiosqlite")."""

import inspect
import re
import tomllib
from importlib import metadata
from pathlib import Path

import aiosqlite

from openchart.store.sqlite import atomic

UPGRADE = (
    "store.sqlite.atomic relies on aiosqlite's private Connection._execute. Read "
    "'Upgrading aiosqlite' in docs/decisions-indicator-alerts.md before changing the pin."
)


def test_execute_exists_with_the_signature_atomic_uses():
    method = getattr(aiosqlite.Connection, "_execute", None)
    assert method is not None, (
        f"aiosqlite {aiosqlite.__version__} has no Connection._execute. {UPGRADE}"
    )
    assert inspect.iscoroutinefunction(method), (
        f"Connection._execute is no longer a coroutine function. {UPGRADE}"
    )
    params = list(inspect.signature(method).parameters.values())
    names = [p.name for p in params]
    assert names[:2] == ["self", "fn"], (
        f"Connection._execute signature is now {names}, expected (self, fn, ...). {UPGRADE}"
    )
    assert any(p.kind is p.VAR_POSITIONAL for p in params) and any(
        p.kind is p.VAR_KEYWORD for p in params
    ), f"Connection._execute no longer takes *args, **kwargs. {UPGRADE}"


async def test_execute_runs_a_function_on_the_database_thread_and_returns_its_result(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "t.db")
    try:
        assert await atomic(conn, lambda db: db.execute("SELECT 41 + 1").fetchone()[0]) == 42, (
            UPGRADE
        )
    finally:
        await conn.close()


def test_installed_aiosqlite_is_the_pinned_version():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    pins = [d for d in pyproject["project"]["dependencies"] if re.match(r"aiosqlite\b", d)]
    assert len(pins) == 1 and re.fullmatch(r"aiosqlite==[\d.]+", pins[0]), (
        f"pyproject.toml must pin aiosqlite exactly (aiosqlite==X.Y.Z), found {pins}. {UPGRADE}"
    )
    installed = metadata.version("aiosqlite")
    assert pins[0] == f"aiosqlite=={installed}", (
        f"pyproject.toml pins {pins[0]} but aiosqlite {installed} is installed. {UPGRADE}"
    )
