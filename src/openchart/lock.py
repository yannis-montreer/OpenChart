"""A lock file in the data folder, held while a server uses it.

Commands that change stored history behind the server's back (`openchart purge`) check
it, whatever port the server listens on. The operating system releases the lock when
the process ends, even after a crash, so a stale file never blocks anything.
"""

from __future__ import annotations

import os
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
