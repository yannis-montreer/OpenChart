"""SQLite storage (WAL mode). One connection per process, used from asyncio."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import aiosqlite

from openchart.store.migrate import UpgradeNeeded, migrate, pending_migrations

T = TypeVar("T")


async def atomic(conn: aiosqlite.Connection, fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run `fn` with the plain connection on the database thread, as one step: every
    coroutine shares the connection, and between two awaited statements another one's
    commit could write the first without the second. In a savepoint: on an exception
    nothing of `fn` stays. Committed at once, unless another coroutine's transaction is
    open: then it is part of that one and is committed with it.

    `_execute` is aiosqlite's own way onto its thread (private, in every version since
    0.17; `tests/test_alert_fire_atomic.py` fails if it goes)."""

    def run() -> T:
        db = conn._conn
        outer = db.in_transaction
        db.execute("SAVEPOINT atomic")
        try:
            result = fn(db)
        except BaseException:
            db.execute("ROLLBACK TO atomic")
            db.execute("RELEASE atomic")
            raise
        db.execute("RELEASE atomic")
        if not outer:
            db.commit()
        return result

    return await conn._execute(run)


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
