"""Deleting one instrument and everything stored for its prices.

Shared by `openchart purge` (server stopped, the folder's lock held by the command) and the
server's Settings, Removed, "Delete data..." (docs/decisions-instrument-delete.md), which
calls it under the instrument's lock with the feed, the cache and the alert engine kept
away from that instrument. Both compact the database afterwards (VACUUM) unless told not
to; the server may find its one connection busy and then retries for a few seconds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from openchart.store.alerts import Alert, AlertStore
from openchart.store.candles import CandleStore
from openchart.store.drawings import Drawing, DrawingStore

# The server's connection may be inside another coroutine's transaction or statement when
# VACUUM comes; it is tried again this often, this far apart, then left for another time.
VACUUM_ATTEMPTS = 100
VACUUM_PAUSE = 0.05


class DeleteRefused(Exception):
    """The server will not delete (yet): `status` is the HTTP answer, the text the reason."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Vacuumed:
    before: int  # bytes of the database and its write-ahead log
    after: int
    seconds: float


@dataclass
class Purged:
    rows: dict[str, int]  # rows deleted per table (CandleStore.purge_instrument)
    drawings: list[Drawing] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    tick_folder: bool = False  # the instrument's tick folder was there
    tick_error: str | None = None  # and could not be deleted (not all of it)
    vacuum: Vacuumed | None = None
    vacuum_error: str | None = None  # VACUUM asked for, but not done


def db_size(path: Path) -> int:
    """The database with its write-ahead log, as the disk sees it."""
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())


async def delete_instrument(
    store: CandleStore,
    ticks_dir: Path,
    db_path: Path,
    source: str,
    instrument: str,
    *,
    drawings_and_alerts: bool,
    vacuum: bool = True,
    before_vacuum: Callable[[int], None] | None = None,
) -> Purged:
    """Delete the instrument's row, candles, cached bars, closures, tick files and its
    chart's indicators (and their revision); with `drawings_and_alerts` also its drawings,
    its alerts and their log. Then VACUUM unless `vacuum` is False (`before_vacuum(size)`
    is called first: the command prints)."""
    conn = store.conn
    purged = Purged(rows=await store.purge_instrument(source, instrument))
    for table in ("chart_indicators", "chart_indicator_revisions"):
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE source=? AND instrument=?", (source, instrument)
        )
        purged.rows[table] = cur.rowcount
    await conn.commit()
    if drawings_and_alerts:
        purged.alerts = await AlertStore(conn).list(instrument)
        purged.drawings = await DrawingStore(conn).list(instrument)
        ids = [a.id for a in purged.alerts]
        marks = ", ".join("?" * len(ids))
        await conn.execute(f"DELETE FROM alert_events WHERE alert_id IN ({marks})", ids)
        await conn.execute(f"DELETE FROM alerts WHERE id IN ({marks})", ids)
        ids = [d.id for d in purged.drawings]
        marks = ", ".join("?" * len(ids))
        await conn.execute(f"DELETE FROM drawings WHERE id IN ({marks})", ids)
        await conn.commit()
    ticks = ticks_dir / source / instrument
    if ticks.exists():
        purged.tick_folder = True
        try:
            await asyncio.to_thread(shutil.rmtree, ticks)
        except OSError as exc:  # a reader holds a file (Windows)
            purged.tick_error = f"{type(exc).__name__}: {exc}"
    if vacuum:
        if before_vacuum is not None:
            before_vacuum(db_size(db_path))
        try:
            purged.vacuum = await compact(conn, db_path)
        except sqlite3.OperationalError as exc:
            purged.vacuum_error = str(exc)
    return purged


async def compact(conn, db_path: Path) -> Vacuumed:
    """VACUUM, then truncate the write-ahead log. SQLite keeps freed pages in the file;
    VACUUM gives them back to the disk. It cannot run inside a transaction or beside an
    unfinished statement on the same connection: in the server another coroutine may hold
    one for a moment, so it is tried again (`VACUUM_ATTEMPTS`) before the error goes up."""
    before = db_size(db_path)
    started = time.monotonic()
    for attempt in range(VACUUM_ATTEMPTS):
        try:
            if conn.in_transaction:
                raise sqlite3.OperationalError("cannot VACUUM from within a transaction")
            await conn.execute("VACUUM")
            break
        except sqlite3.OperationalError:
            if attempt == VACUUM_ATTEMPTS - 1:
                raise
            await asyncio.sleep(VACUUM_PAUSE)
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return Vacuumed(before, db_size(db_path), time.monotonic() - started)


async def fingerprint(store: CandleStore, ticks_dir: Path, source: str, instrument: str) -> str:
    """What a delete would remove, condensed: the preview's token. Any new candle, cached
    bar, closure, tick file, drawing, alert or alert event changes it, so a delete carrying
    an older token is refused instead of removing something its preview did not show."""
    conn = store.conn

    async def rows(sql: str, *args) -> list[list]:
        return [list(r) for r in await conn.execute_fetchall(sql, args)]

    parts: dict[str, object] = {
        "instrument": await rows(
            "SELECT * FROM instruments WHERE source=? AND instrument=?", source, instrument
        ),
        "closures": await rows(
            "SELECT COUNT(*), MIN(start_ts), MAX(end_ts) FROM closures"
            " WHERE source=? AND instrument=?",
            source,
            instrument,
        ),
        "drawings": await rows(
            "SELECT id, updated_at FROM drawings WHERE instrument=? ORDER BY id", instrument
        ),
        "alerts": await rows(
            "SELECT id, status, updated_at FROM alerts WHERE instrument=? ORDER BY id", instrument
        ),
        "events": await rows(
            "SELECT COUNT(*), MAX(e.id) FROM alert_events e JOIN alerts a ON a.id = e.alert_id"
            " WHERE a.instrument=?",
            instrument,
        ),
        "candles": await store.extent(source, instrument),
        "indicators": await rows(
            "SELECT position, uid, kind, indicator, params, visible, price_labels, legend_values"
            " FROM chart_indicators WHERE source=? AND instrument=? ORDER BY position",
            source,
            instrument,
        ),
        "indicator_revision": await rows(
            "SELECT revision FROM chart_indicator_revisions WHERE source=? AND instrument=?",
            source,
            instrument,
        ),
    }
    parts["ticks"] = await asyncio.to_thread(_tick_files, ticks_dir / source / instrument)
    text = json.dumps([source, instrument, parts], sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _tick_files(folder: Path) -> list[tuple[str, int, int]]:
    found = []
    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            found.append((os.path.relpath(path, folder), st.st_size, st.st_mtime_ns))
    return sorted(found)


class InstrumentLocks:
    """One lock per instrument, held by the server's work that changes what is stored for
    an instrument outside the feed's own writes: adding it (back), removing it, changing
    its minute history, the repair before an alert catch-up, the daily retention, and the
    delete. `busy` says what holds it, so a delete can refuse instead of queueing."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._what: dict[str, str] = {}

    @asynccontextmanager
    async def hold(self, instrument: str, what: str) -> AsyncIterator[None]:
        async with self._locks[instrument]:
            self._what[instrument] = what
            try:
                yield
            finally:
                self._what.pop(instrument, None)

    def busy(self, instrument: str) -> str | None:
        return self._what.get(instrument)
