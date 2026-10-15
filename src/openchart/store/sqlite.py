"""SQLite storage (WAL mode). One connection per process, used from asyncio."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from openchart.store.migrate import UpgradeNeeded, migrate, pending_migrations


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str | Path, *, upgrade: bool = True) -> Database:
        """Open (and create) the database. With `upgrade`, pending migrations run; without
        it, pending migrations raise `UpgradeNeeded` instead (a server is running on it
        and must not have its database changed underneath)."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            if upgrade:
                await migrate(conn)
            elif pending := await pending_migrations(conn):
                names = ", ".join(f"{m.version:04d} {m.name}" for m in pending)
                raise UpgradeNeeded(f"the database needs upgrading ({names})")
        except BaseException:
            await conn.close()
            raise
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def __aenter__(self) -> Database:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
