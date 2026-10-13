"""`openchart backup`: one zip file holding the data folder, safe while the server runs.

The database is copied with SQLite's online backup API, which reads one consistent
snapshot even while the server writes (in WAL mode writers are not blocked), and the copy
is checked (`PRAGMA quick_check`) before it goes into the zip. The other files (tick
Parquet files, your indicators, the encrypted secrets file) are written once and replaced
atomically by the server, so they are copied as they are; a tick part file merged away
during the copy is skipped, its ticks are in the day's merged file.

The secrets file is encrypted and its key is not in the data folder, so a backup does not
expose the tokens; restoring it needs the same key (or the tokens entered again).
"""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openchart import __version__

DB_NAME = "openchart.db"
BACKUP_DIR = "backups"
LOG_DIR = "logs"
# Never copied: the live database files (the snapshot replaces them) and leftovers.
SKIP_SUFFIXES = (".db-wal", ".db-shm", ".tmp", ".part")


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    size: int
    files: int
    tick_files: int
    skipped: list[str] = field(default_factory=list)
    has_secrets: bool = False


def backup(
    data_dir: Path, out_dir: Path | None = None, *, ticks: bool = True, now: float | None = None
) -> BackupResult:
    data_dir = data_dir.resolve()
    db_path = data_dir / DB_NAME
    if not db_path.is_file():
        raise BackupError(f"No database in {data_dir} (looked for {DB_NAME}).")
    out_dir = (out_dir or data_dir / BACKUP_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Local time, like the other files in the folder listing.
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"openchart-backup-{stamp}.zip"
    partial = target.with_suffix(".zip.part")
    snapshot = out_dir / f".snapshot-{stamp}.db"

    try:
        _snapshot(db_path, snapshot)
        result = BackupResult(path=target, size=0, files=1, tick_files=0)
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(snapshot, DB_NAME)
            for path in sorted(data_dir.rglob("*")):
                rel = path.relative_to(data_dir)
                if not path.is_file() or _skipped(rel, path, data_dir, out_dir):
                    continue
                is_tick = rel.parts[0] == "ticks"
                if is_tick and not ticks:
                    continue
                try:
                    # Parquet is already compressed: store it as it is.
                    kind = zipfile.ZIP_STORED if path.suffix == ".parquet" else zipfile.ZIP_DEFLATED
                    zf.write(path, rel.as_posix(), compress_type=kind)
                except FileNotFoundError:  # merged or replaced while we copied
                    result.skipped.append(rel.as_posix())
                    continue
                result.files += 1
                result.tick_files += is_tick
                result.has_secrets |= rel.as_posix() == "secrets.enc"
            zf.writestr(
                "backup.json",
                json.dumps(
                    {
                        "openchart": __version__,
                        "created": stamp,
                        "data_dir": str(data_dir),
                        "files": result.files,
                        "ticks_included": ticks,
                    },
                    indent=2,
                ),
            )
        partial.replace(target)
        result.size = target.stat().st_size
        return result
    finally:
        snapshot.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def _snapshot(db_path: Path, dest: Path) -> None:
    """A consistent copy of the live database, checked."""
    dest.unlink(missing_ok=True)
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)  # one step: a single read transaction, so one consistent snapshot
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"The database copy failed its check: {check}")
        dst.execute("PRAGMA journal_mode=DELETE")  # a self-contained file, no -wal next to it
    finally:
        dst.close()
        src.close()


def _skipped(rel: Path, path: Path, data_dir: Path, out_dir: Path) -> bool:
    if rel.parts[0] in (BACKUP_DIR, LOG_DIR) or rel.as_posix() == DB_NAME:
        return True
    if path.name.endswith(SKIP_SUFFIXES) or path.name.startswith(".snapshot-"):
        return True
    return out_dir == path.parent or out_dir in path.parents  # a custom --out inside data


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{n} bytes"
