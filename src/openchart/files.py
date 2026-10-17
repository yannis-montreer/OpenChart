"""Files only their owner may read (the encrypted tokens and backups), and moves that
wait out a reader on Windows.

On Linux and macOS (Docker) they are created with mode 0600 from the start, so they are
never readable by others, not even for a moment; the process's umask (0022 in the image)
would otherwise make them 0644. On Windows the mode bits do not apply: the data folder's
ACL (set by install-service.ps1) is what protects them.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO

PRIVATE_MODE = 0o600


def open_private(path: Path) -> BinaryIO:
    """Create (or truncate) `path` for writing, readable by its owner only."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, PRIVATE_MODE)
    if os.name != "nt":
        os.fchmod(fd, PRIVATE_MODE)  # an existing file keeps its old mode otherwise
    return os.fdopen(fd, "wb")


SHARING_WAIT = 1.0  # seconds a move waits for a reader that holds the file (Windows)
_IN_USE = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION


def move(src: Path, dst: Path, *, overwrite: bool = True, wait: float = SHARING_WAIT) -> None:
    """`os.replace` (or `os.rename` without `overwrite`), retried while Windows says the
    file is in use: a file another thread or process has open (a catalog read, the file
    being described) cannot be renamed there for that moment. Elsewhere, and for any
    other error, it fails at once."""
    end = time.monotonic() + wait
    while True:
        try:
            (os.replace if overwrite else os.rename)(src, dst)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in _IN_USE:
                raise
            if time.monotonic() >= end:
                raise
            time.sleep(0.02)
