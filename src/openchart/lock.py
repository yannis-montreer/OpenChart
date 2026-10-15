"""A lock file in the data folder, held while a server uses it.

The server holds it while it runs, and every command that writes holds it while it works
(`exclusive`), so the two never change the data behind each other's back: one refuses
with an explanation instead. The file stays when nobody holds the lock; the operating
system releases the lock when the process ends, even after a crash, so a stale file
never blocks anything.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = "server.lock"


class DataFolderLock:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting. False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None


class DataFolderInUse(RuntimeError):
    """Another OpenChart process (a server, or a command that writes) holds the folder."""


def in_use_message(data_dir: Path, what: str) -> str:
    return (
        f"OpenChart is running on {data_dir} (another process holds {Path(data_dir) / LOCK_NAME}): "
        f"stop it first, then {what} again. Nothing was changed."
    )


@contextmanager
def exclusive(data_dir: Path, what: str) -> Iterator[DataFolderLock]:
    """Hold the folder's lock for a command that writes; refuse if someone else holds it.
    `what` names the command for the message ("run openchart migrate")."""
    lock = DataFolderLock(data_dir)
    if not lock.acquire():
        raise DataFolderInUse(in_use_message(data_dir, what))
    try:
        yield lock
    finally:
        lock.release()


def in_use(data_dir: Path) -> bool:
    """Is a server running on this data folder?"""
    lock = DataFolderLock(data_dir)
    if not lock.path.exists():
        return False
    if lock.acquire():
        lock.release()
        return False
    return True


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
