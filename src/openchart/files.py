"""Files only their owner may read: the encrypted tokens and backups.

On Linux and macOS (Docker) they are created with mode 0600 from the start, so they are
never readable by others, not even for a moment; the process's umask (0022 in the image)
would otherwise make them 0644. On Windows the mode bits do not apply: the data folder's
ACL (set by install-service.ps1) is what protects them.
"""

from __future__ import annotations

import os
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
