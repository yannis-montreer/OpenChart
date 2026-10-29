"""What is stored for one instrument, to show before it is deleted or added back."""

import os
from dataclasses import dataclass
from pathlib import Path

from openchart.store.candles import CandleStore


@dataclass(frozen=True)
class Stored:
    m1: int
    h1: int
    cached: int
    closures: int
    drawings: int
    alerts: int
    active_alerts: int
    tick_files: int
    tick_bytes: int
    #: Candle rows' share of the database file (see `stored_summary`) plus the tick files.
    bytes: int

    @property
    def candles(self) -> int:
        return self.m1 + self.h1 + self.cached


def tick_folder_size(folder: Path) -> tuple[int, int]:
    """(files, bytes) under a tick folder; (0, 0) when there is none."""
    files = size = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            files += 1
    return files, size


async def stored_summary(
    store: CandleStore, ticks_dir: Path, source: str, instrument: str
) -> Stored:
    """Counts and an estimated size. The candles' bytes are the instrument's share of the
    candle rows times the database's used pages: exact sizes would need a scan, and the
    figure is only there to say "about this much". The tick files are measured."""
    conn = store.conn

    async def one(sql: str, *args) -> int:
        async with conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return (row[0] or 0) if row else 0

    m1, h1, cached, everything = await store.row_counts(source, instrument)
    pages = await one("PRAGMA page_count") - await one("PRAGMA freelist_count")
    page_size = await one("PRAGMA page_size")
    candle_bytes = pages * page_size * (m1 + h1 + cached) // everything if everything else 0
    files, tick_bytes = tick_folder_size(ticks_dir / source / instrument)
    return Stored(
        m1=m1,
        h1=h1,
        cached=cached,
        closures=await one(
            "SELECT COUNT(*) FROM closures WHERE source=? AND instrument=?", source, instrument
        ),
        drawings=await one("SELECT COUNT(*) FROM drawings WHERE instrument=?", instrument),
        alerts=await one("SELECT COUNT(*) FROM alerts WHERE instrument=?", instrument),
        active_alerts=await one(
            "SELECT COUNT(*) FROM alerts WHERE instrument=? AND status='active'", instrument
        ),
        tick_files=files,
        tick_bytes=tick_bytes,
        bytes=candle_bytes + tick_bytes,
    )
