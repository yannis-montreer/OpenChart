"""Numbered SQL migrations.

Each file in `store/migrations/` is named `NNNN_description.sql`. Files are applied in
order, each in its own transaction, and recorded in `schema_migrations`. Applied
migrations are never edited; schema changes go in a new file.

A migration containing the line `-- vacuum-after` (one that rewrites big tables) is
followed by VACUUM, outside its transaction, so the space it freed goes back to the disk.
Such a migration first checks that the disk has room for it: the rewritten tables, the
write-ahead log and VACUUM's temporary copy took up to 1.4 times the database on the
owner's data, so it asks for 1.5 times the database (and its WAL) free, and otherwise
stops before changing anything.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import aiosqlite

_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
VACUUM_AFTER = "-- vacuum-after"
# Free disk needed by a vacuum-after migration, as a multiple of the database's size.
SPACE_FACTOR = 1.5


class MigrationError(RuntimeError):
    """A migration cannot run; nothing was changed."""


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
    pending = [m for m in migrations if m.version not in done]
    if any(VACUUM_AFTER in m.sql for m in pending):
        await check_free_space(conn, [m.version for m in pending if VACUUM_AFTER in m.sql])
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


async def check_free_space(conn: aiosqlite.Connection, versions: list[int]) -> None:
    """Refuse to start a big rewrite without room for it on the database's disk."""
    async with conn.execute("PRAGMA database_list") as cur:
        files = [row[2] for row in await cur.fetchall() if row[1] == "main"]
    if not files or not files[0]:
        return  # an in-memory database
    db = Path(files[0])
    size = sum(p.stat().st_size for p in (db, Path(f"{db}-wal")) if p.exists())
    need = int(size * SPACE_FACTOR)
    free = shutil.disk_usage(db.parent).free
    if free < need:
        raise MigrationError(
            f"Not enough free disk space to upgrade the database (migration "
            f"{', '.join(map(str, versions))}): it needs about {need / 1e6:,.0f} MB free on "
            f"the disk of {db.parent}, and {free / 1e6:,.0f} MB are free. Nothing was "
            "changed. Free some space and start OpenChart again."
        )
