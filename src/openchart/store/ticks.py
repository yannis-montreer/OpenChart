"""Raw tick storage in Parquet.

Layout: `{root}/{source}/{instrument}/date=YYYY-MM-DD/part-*.parquet`. Ticks are
buffered in memory and flushed as a new part file, so a crash loses at most one flush
interval and never corrupts earlier files. Once a UTC day is over, its parts are
compacted into a single `data.parquet`. The directory is a hive-partitioned dataset:
`pyarrow.dataset.dataset(path, partitioning="hive")` reads it directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from openchart.providers.base import Tick

log = logging.getLogger(__name__)

SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
    ]
)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _write_atomic(table: pa.Table, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


class TickWriter:
    def __init__(self, root: str | Path, source: str):
        self.root = Path(root)
        self.source = source
        self._buffer: dict[tuple[str, str], list[Tick]] = defaultdict(list)
        self._seq = 0
        # Flush, compaction and retention run in threads: one at a time on the files.
        self._files = threading.Lock()

    def day_dir(self, instrument: str, day: str) -> Path:
        return self.root / self.source / instrument / f"date={day}"

    def add(self, tick: Tick) -> None:
        self._buffer[(tick.instrument, _day(tick.ts))].append(tick)

    def buffered(self, instrument: str) -> list[Tick]:
        """Ticks for `instrument` not flushed to disk yet, in arrival order."""
        out: list[Tick] = []
        for (name, _day), ticks in sorted(self._buffer.items()):
            if name == instrument:
                out.extend(ticks)
        return out

    @property
    def pending(self) -> int:
        return sum(len(v) for v in self._buffer.values())

    def flush(self) -> int:
        """Write buffered ticks as new part files. Blocking; call via a thread."""
        with self._files:
            return self._flush()

    def _flush(self) -> int:
        buffer, self._buffer = self._buffer, defaultdict(list)
        written = 0
        for (instrument, day), ticks in buffer.items():
            directory = self.day_dir(instrument, day)
            directory.mkdir(parents=True, exist_ok=True)
            table = pa.table(
                {
                    "ts": pa.array([int(t.ts * 1_000_000) for t in ticks], SCHEMA.field("ts").type),
                    "bid": [t.bid for t in ticks],
                    "ask": [t.ask for t in ticks],
                },
                schema=SCHEMA,
            )
            self._seq += 1
            name = f"part-{time.time_ns()}-{self._seq:06d}.parquet"
            _write_atomic(table, directory / name)
            written += len(ticks)
        return written

    def compact_closed_days(self, today: str | None = None) -> int:
        """Merge part files of finished UTC days into one `data.parquet`. Blocking."""
        with self._files:
            return self._compact(today)

    def _compact(self, today: str | None) -> int:
        today = today or _day(time.time())
        compacted = 0
        for directory in self.root.glob(f"{self.source}/*/date=*"):
            day = directory.name.removeprefix("date=")
            parts = sorted(directory.glob("part-*.parquet"))
            if day >= today or not parts:
                continue
            existing = directory / "data.parquet"
            files = [*parts, existing] if existing.exists() else parts
            table = pa.concat_tables([pq.read_table(f, schema=SCHEMA) for f in files])
            # Dedupe covers a crash between writing data.parquet and deleting the parts.
            table = table.group_by(["ts", "bid", "ask"]).aggregate([])
            table = table.select(SCHEMA.names).cast(SCHEMA).sort_by("ts")
            _write_atomic(table, directory / "data.parquet")
            for part in parts:
                part.unlink()
            compacted += 1
        return compacted

    def delete_expired(self, now: float, days: int) -> list:
        """Delete the day folders whose whole UTC day ended at or before `now - days`
        (`retention.expired_tick_days`), one log line each. Blocking; call via a thread."""
        from openchart.retention import expired_tick_days

        gone = []
        with self._files:
            for d in expired_tick_days(self.root, self.source, now, days):
                try:
                    shutil.rmtree(d.path)
                except OSError as exc:  # a reader holds a file (Windows): next time
                    log.warning(
                        "retention: could not delete ticks of %s %s: %s", d.instrument, d.day, exc
                    )
                    continue
                log.info(
                    "retention: ticks of %s %s deleted (%d file(s), %.1f MB; kept %d days)",
                    d.instrument,
                    d.day,
                    d.files,
                    d.bytes / 1e6,
                    days,
                )
                gone.append(d)
        return gone


def read_ticks(root: str | Path, source: str, instrument: str, day: str) -> pa.Table:
    directory = Path(root) / source / instrument / f"date={day}"
    files = sorted(directory.glob("*.parquet"))
    if not files:
        return SCHEMA.empty_table()
    return pa.concat_tables([pq.read_table(f, schema=SCHEMA) for f in files]).sort_by("ts")
