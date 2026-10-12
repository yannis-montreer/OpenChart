"""SQLite storage (WAL mode). One connection per process, used from asyncio."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from openchart.store.migrate import migrate


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> Database:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await migrate(conn)
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def __aenter__(self) -> Database:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
