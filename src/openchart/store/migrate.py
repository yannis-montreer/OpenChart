"""Numbered SQL migrations.

Each file in `store/migrations/` is named `NNNN_description.sql`. Files are applied in
order, each in its own transaction, and recorded in `schema_migrations`. Applied
migrations are never edited; schema changes go in a new file.

A migration containing the line `-- vacuum-after` (one that rewrites big tables) is
followed by VACUUM, outside its transaction, so the space it freed goes back to the disk.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from importlib import resources

import aiosqlite

_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
VACUUM_AFTER = "-- vacuum-after"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover_migrations(package: str = "openchart.store.migrations") -> list[Migration]:
    found: list[Migration] = []
    for entry in resources.files(package).iterdir():
        match = _NAME_RE.match(entry.name)
        if match:
            found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration numbers: {versions}")
    return found


async def applied_versions(conn: aiosqlite.Connection) -> set[int]:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        return {row[0] for row in await cur.fetchall()}


async def migrate(
    conn: aiosqlite.Connection, migrations: list[Migration] | None = None
) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    if migrations is None:
        migrations = discover_migrations()
    done = await applied_versions(conn)
    await conn.commit()
    newly: list[int] = []
    for m in migrations:
        if m.version in done:
            continue
        script = (
            "BEGIN;\n"
            f"{m.sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({m.version}, '{m.name}', {int(time.time())});\n"
            "COMMIT;"
        )
        try:
            await conn.executescript(script)
        except Exception:
            await conn.rollback()
            raise
        newly.append(m.version)
    if any(VACUUM_AFTER in m.sql for m in migrations if m.version in newly):
        await conn.execute("VACUUM")
    return newly
