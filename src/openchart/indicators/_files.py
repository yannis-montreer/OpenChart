"""Your indicator files on disk, for the editor (CI-4): read, create, save with a history,
retitle. Only `<id>.py` directly in the indicators folder, `.history/<id>/` and the
temporary files of a write are ever touched (docs/api.md, "Your indicator definitions").

The rules, each against a way a name or a file could reach elsewhere:

- **ids** match `[a-z][a-z0-9_]{0,39}` as a whole and are not a Windows device name
  (`con`, `nul`, `com1` ...: on Windows `con.py` is the console, not a file);
- **plain files only:** a definition, a history folder or a version that is a link, a
  junction or anything but a plain file or folder is refused, never followed or replaced;
- **atomic writes:** a temporary file in the folder, then one rename (`os.replace`, or a
  rename that cannot overwrite for a new file), so a reader or a crash sees the old file or
  the new one, never half of one;
- **sources** are at most 256 KB as UTF-8, without NUL; a leading byte-order mark is dropped.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from openchart.indicators._base import MAX_FILE_BYTES

HISTORY_KEEP = 10  # versions per definition (design 1.1)
ID = re.compile(r"[a-z][a-z0-9_]{0,39}")  # with fullmatch only
DEVICES = {"con", "prn", "aux", "nul", "conin", "conout"}
DEVICES |= {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}
VERSION = re.compile(r"(\d{8}T\d{6}Z)(?:-([2-9]|[1-9]\d+))?")  # with fullmatch only
TITLE_START = re.compile(r"^TITLE\b")
TITLE_LINE = re.compile(
    r"^TITLE[ \t]*=[ \t]*"
    r"(?P<lit>\"(?:[^\"\\\r\n]|\\.)*\"|'(?:[^'\\\r\n]|\\.)*')"
    r"(?P<rest>[ \t]*(?:#[^\r\n]*)?)(?P<end>\r?\n?)$"
)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class FileProblem(Exception):
    """A request the files cannot serve: `status` and a message (or a detail dict)."""

    def __init__(self, status: int, detail: str | dict):
        self.status, self.detail = status, detail
        super().__init__(str(detail))


def valid_id(text: object) -> bool:
    return isinstance(text, str) and bool(ID.fullmatch(text)) and text not in DEVICES


def version_of(text: str) -> str:
    """The first 12 hex digits of the source's SHA-256 (as `checks[].version`)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def clean_source(text: object) -> str:
    """The source as it will be stored, or FileProblem (400 or 413)."""
    if not isinstance(text, str):
        raise FileProblem(400, "source must be text")
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\x00" in text:
        raise FileProblem(400, "source holds a NUL character")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise FileProblem(400, "source is not valid Unicode (a lone surrogate)") from None
    if size > MAX_FILE_BYTES:
        raise FileProblem(413, f"source is {size:,} bytes; at most {MAX_FILE_BYTES:,}")
    return text


def _is_link(path: Path) -> bool:
    """A symbolic link, or (Windows) any reparse point such as a junction."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _plain_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and not _is_link(path) and st.st_nlink <= 1


class Files:
    """The indicators folder, as the editor's endpoints use it."""

    def __init__(self, folder: Path):
        self.folder = folder

    # Paths ---------------------------------------------------------------------------

    def path(self, indicator_id: str) -> Path:
        if not valid_id(indicator_id):
            raise FileProblem(404, f"no indicator of yours named {indicator_id!r}")
        return self.folder / f"{indicator_id}.py"

    def exists(self, indicator_id: str) -> bool:
        """Is the name taken by anything (a file, a folder, a link, even a broken one)?"""
        return os.path.lexists(self.path(indicator_id))

    def existing(self, indicator_id: str) -> Path:
        """`<id>.py` as a plain file: 404 if there is none, 409 if it is something else."""
        path = self.path(indicator_id)
        if not os.path.lexists(path):
            raise FileProblem(404, f"no indicator of yours named {indicator_id!r}")
        if not _plain_file(path):
            raise FileProblem(409, f"{path.name} is not a plain file (a link, a folder ...)")
        return path

    def free_id(self, base: str) -> str:
        """The first of `<base>_2`, `<base>_3` ... that no name takes, cut to stay an id."""
        for n in range(2, 10_000):
            suffix = f"_{n}"
            candidate = base[: 40 - len(suffix)] + suffix
            if valid_id(candidate) and not os.path.lexists(self.folder / f"{candidate}.py"):
                return candidate
        raise FileProblem(409, f"no free id like {base}_2")

    def _history_dir(self, indicator_id: str, *, create: bool) -> Path | None:
        """`.history/<id>/`, a plain folder inside this one (409 otherwise); None if it
        does not exist and `create` is false."""
        root = self.folder.resolve()
        top, mine = self.folder / ".history", self.folder / ".history" / indicator_id
        for p in (top, mine):
            if not os.path.lexists(p):
                if not create:
                    return None
                p.mkdir()
            if _is_link(p) or not p.is_dir():
                raise FileProblem(409, f"{p.relative_to(self.folder)} is not a plain folder")
        if mine.resolve().parent != root / ".history":
            raise FileProblem(409, f".history/{indicator_id} is not inside {self.folder}")
        return mine

    # Reading -------------------------------------------------------------------------

    def read(self, indicator_id: str) -> str:
        path = self.existing(indicator_id)
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise FileProblem(409, f"{path.name} is not UTF-8 text") from None

    def versions(self, indicator_id: str) -> list[tuple[str, Path]]:
        """(version, path), newest first."""
        folder = self._history_dir(self.existing(indicator_id).stem, create=False)
        if folder is None:
            return []
        found = []
        for p in folder.iterdir():
            m = VERSION.fullmatch(p.stem) if p.suffix == ".py" else None
            if m and _plain_file(p):
                found.append(((m.group(1), int(m.group(2) or 1)), p.stem, p))
        found.sort(reverse=True)
        return [(v, p) for _, v, p in found]

    def version(self, indicator_id: str, version: str) -> Path:
        if not VERSION.fullmatch(version):
            raise FileProblem(404, f"no version {version!r}")
        for v, p in self.versions(indicator_id):
            if v == version:
                return p
        raise FileProblem(404, f"no version {version!r}")

    # Writing -------------------------------------------------------------------------

    def _temporary(self, data: bytes) -> Path:
        tmp = self.folder / f".{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def create(self, indicator_id: str, text: str) -> None:
        """A new `<id>.py`; 409 if the name is taken, also by a write at the same moment."""
        path = self.path(indicator_id)
        if os.path.lexists(path):
            raise FileProblem(
                409, {"message": f"{path.name} exists", "suggested": self.free_id(indicator_id)}
            )
        tmp = self._temporary(text.encode("utf-8"))
        try:
            if sys.platform == "win32":
                os.rename(tmp, path)  # fails if the name exists: never overwrites
            else:
                os.link(tmp, path)  # the same, atomically, on POSIX
                tmp.unlink()
        except FileExistsError:
            raise FileProblem(
                409, {"message": f"{path.name} exists", "suggested": self.free_id(indicator_id)}
            ) from None
        except OSError as exc:
            raise FileProblem(409, f"could not write {path.name}: {exc}") from None
        finally:
            tmp.unlink(missing_ok=True)

    def save(self, indicator_id: str, text: str, *, base_version: str | None = None) -> bool:
        """Replace `<id>.py` with `text`, the old one kept in the history first. Returns
        False when `text` is what is stored (nothing written). 409 when the stored file is
        not `base_version` any more, or cannot be written."""
        path = self.existing(indicator_id)
        current = self.read(indicator_id)
        if base_version is not None and version_of(current) != base_version:
            raise FileProblem(
                409,
                {
                    "message": f"{path.name} was changed since it was opened",
                    "version": version_of(current),
                },
            )
        if current == text:
            return False
        folder = self._history_dir(indicator_id, create=True)
        assert folder is not None
        kept = self._keep(folder, current)
        tmp = self._temporary(text.encode("utf-8"))
        try:
            os.replace(tmp, path)
        except OSError as exc:
            kept.unlink(missing_ok=True)  # the save did not happen: no version for it
            raise FileProblem(409, f"could not write {path.name}: {exc}") from None
        finally:
            tmp.unlink(missing_ok=True)
        self._prune(folder)
        return True

    def _keep(self, folder: Path, text: str) -> Path:
        stamp = datetime.fromtimestamp(time.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
        for n in range(1, 10_000):
            target = folder / (f"{stamp}.py" if n == 1 else f"{stamp}-{n}.py")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(target, flags, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))
            return target
        raise FileProblem(409, "too many versions in one second")

    def _prune(self, folder: Path) -> None:
        stem = folder.name
        for _, p in self.versions(stem)[HISTORY_KEEP:]:
            p.unlink(missing_ok=True)


# Names and titles --------------------------------------------------------------------------


def id_from_filename(filename: str | None) -> str:
    """`My RSI.py` -> `my_rsi`: the base name without `.py`, accents dropped, lowercased,
    other characters to `_`, an `x` before a leading digit or `_`, cut to 40; `imported`
    when nothing is left, `<name>_` for a Windows device name."""
    if not filename:
        return "imported"
    name = re.split(r"[\\/]", filename)[-1]
    if name.lower().endswith(".py"):
        name = name[:-3]
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch)).lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).rstrip("_")
    if not name.strip("_"):
        return "imported"
    if not name[0].isalpha():
        name = "x" + name
    name = name[:40]
    return name + "_" if name in DEVICES else name


def title_of(text: str) -> str | None:
    """The `TITLE = "..."` literal when the file has exactly one, on one line."""
    found = _title_line(text)
    return None if found is None else found[1]


def _title_line(text: str) -> tuple[int, str] | None:
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if TITLE_START.match(ln)]
    if len(starts) != 1:
        return None
    m = TITLE_LINE.match(lines[starts[0]])
    if m is None:
        return None
    try:
        value = ast.literal_eval(m.group("lit"))
    except (ValueError, SyntaxError):
        return None
    return (starts[0], value) if isinstance(value, str) else None


def with_title(text: str, title: str) -> str:
    """`text` with its one TITLE line saying `title`; FileProblem 409 if it has no such
    line (design 1.1: "edit TITLE in the code")."""
    found = _title_line(text)
    if found is None:
        raise FileProblem(409, {"message": "edit TITLE in the code"})
    lines = text.splitlines(keepends=True)
    m = TITLE_LINE.match(lines[found[0]])
    assert m is not None
    lines[found[0]] = (
        f"TITLE = {json.dumps(title, ensure_ascii=False)}{m.group('rest')}{m.group('end')}"
    )
    return "".join(lines)


def valid_title(title: object) -> str:
    if not isinstance(title, str) or not 1 <= len(title) <= 60:
        raise FileProblem(400, "title: 1 to 60 characters")
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in title):
        raise FileProblem(400, "title: no line breaks or control characters")
    return title
