"""The indicator worker: the process custom indicator code runs in (`openchart
indicator-worker`, docs/custom-indicators.md).

It is started by the server with less than the server can do (`windows.py`, `posix.py`)
and talks to it in frames (`protocol.py`) over stdin and stdout, or over the sidecar's
socket in Docker. It is sent source text and bars, never a path, compiles each script
under its own file name so tracebacks name the right file and line, and answers with the
declared schema, the warmup, or the computed values.

Before it is armed it answers probes (`isolation.py`: what can this process reach?).
Arming installs an audit hook that refuses files, sockets, programs and ctypes with a
readable message, so honest mistakes fail clearly. Python's audit hooks are not a
sandbox: the operating system's limits around this process are the protection.

Only the standard library is imported before the paths are set up: a source install
starts the base interpreter in isolated mode and passes the package and site folders.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import traceback
import types
from typing import Any

T0 = time.perf_counter()

NEED_SOURCE = "NeedSource"
PROBE_READ_BYTES = 16


# Starting -------------------------------------------------------------------------------


def _setup_paths(argv: list[str]) -> dict[str, Any]:
    """`--path <folder>` goes first on sys.path (the openchart package the server itself
    runs), each `--site <folder>` is added like site-packages (a venv's packages, with its
    .pth files), `--listen <socket>` makes this the Docker sidecar."""
    opts: dict[str, Any] = {"path": [], "site": [], "listen": None, "parent": None}
    it = iter(argv)
    for arg in it:
        if arg in ("--path", "--site"):
            opts[arg[2:]].append(next(it))
        elif arg == "--listen":
            opts["listen"] = next(it)
        elif arg == "--parent":
            opts["parent"] = int(next(it))
        else:
            raise SystemExit(f"indicator-worker: unknown argument {arg!r}")
    if opts["site"]:
        import site

        for folder in opts["site"]:
            site.addsitedir(folder)
    sys.path[:0] = opts["path"]
    return opts


def main(argv: list[str] | None = None) -> int:
    opts = _setup_paths(list(sys.argv[1:] if argv is None else argv))
    if opts["listen"]:
        # The sidecar container inherits the image's environment (OPENCHART_DATA_DIR...):
        # scripts see none of it.
        keep = {k: os.environ[k] for k in ("PATH", "LANG", "HOME") if k in os.environ}
        os.environ.clear()
        os.environ.update(keep, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    # Heavy imports after the paths: numpy, pandas and the indicator interface.
    worker = Worker()
    if opts["listen"]:
        from openchart.indicators.worker.posix import serve_socket

        return serve_socket(opts["listen"], worker)
    return serve_pipes(worker, opts["parent"])


def serve_pipes(worker: Worker, parent: int | None) -> int:
    from openchart.indicators.worker.protocol import ProtocolError, encode, exact_reader, read_frame

    fin, fout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a script's print() goes to the log, never between frames
    if parent and os.name != "nt":  # on Windows the job object ends the worker
        _watch_parent(parent)
    read = exact_reader(fin)
    fout.write(encode(worker.hello()))
    fout.flush()
    while True:
        try:
            req, arrays = read_frame(read)
        except EOFError:
            return 0
        except ProtocolError as exc:
            sys.stderr.write(f"indicator worker: {exc}\n")
            return 2
        if req.get("cmd") == "exit":
            return 0
        head, out = worker.handle(req, arrays)
        fout.write(encode(head, out))
        fout.flush()


def _watch_parent(parent: int) -> None:
    """If the server dies, so does the worker (Linux and macOS have no job object)."""

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1)
        os._exit(1)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


# The work -------------------------------------------------------------------------------


class Worker:
    """Requests, by `cmd`: probe, arm, describe, warmup, compute (and exit)."""

    def __init__(self) -> None:
        import numpy  # noqa: F401 (imported before arming, with everything it loads)
        import pandas  # noqa: F401

        import openchart.indicators  # noqa: F401
        from openchart.indicators import _registry  # noqa: F401

        self.import_s = time.perf_counter() - T0
        self.armed = False
        self.sources: dict[str, str] = {}  # hash -> source
        self.modules: dict[tuple[str, str], Any] = {}  # (hash, file) -> Indicator or its error

    def hello(self) -> dict[str, Any]:
        return {
            "ready": True,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
            "import_s": round(self.import_s, 3),
        }

    def handle(self, req: dict[str, Any], arrays: dict[str, Any]) -> tuple[dict, dict]:
        """The answer to one request, carrying the request's id (`rid`, S6): the server
        refuses an answer whose id is not the one it sent, so a frame a script writes on
        the stream fails loudly instead of being read as another request's answer."""
        head, out = self._handle(req, arrays)
        if "rid" in req:
            head = {**head, "rid": req["rid"]}
        return head, out

    def _handle(self, req: dict[str, Any], arrays: dict[str, Any]) -> tuple[dict, dict]:
        cmd = req.get("cmd")
        try:
            if cmd == "probe":
                if self.armed:
                    return {"error": _error_info("RuntimeError", "already armed")}, {}
                from openchart.indicators.worker.probes import run_probes

                return {"probes": run_probes(req.get("targets") or {})}, {}
            if cmd == "arm":
                self.arm(int(req.get("memory_mb") or 0))
                return {"armed": True}, {}
            if not self.armed:
                return {"error": _error_info("RuntimeError", "the worker is not armed")}, {}
            if cmd in ("describe", "warmup", "compute"):
                if req.get("source") is not None:
                    self.learn(req)
                if req["hash"] not in self.sources:
                    return {"error": _error_info(NEED_SOURCE, "send the source")}, {}
                indicator = self.load(req["hash"], req.get("file"))
                if cmd == "describe":
                    return {"definition": indicator.to_json()}, {}
                params = indicator.normalize(req.get("params") or {})
                if cmd == "warmup":
                    return {"warmup": int(indicator.warmup(params))}, {}
                return self.compute(indicator, params, arrays, req.get("info"))
            return {"error": _error_info("ValueError", f"unknown command {cmd!r}")}, {}
        except BaseException as exc:  # a script's own error: report it, keep serving
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) and cmd not in (
                "describe",
                "warmup",
                "compute",
            ):
                raise
            return {"error": self.describe_error(exc, req)}, {}

    def arm(self, memory_mb: int) -> None:
        if self.armed:
            return
        if memory_mb and os.name != "nt":  # Windows: the job object holds the limit
            from openchart.indicators.worker.posix import limit_memory

            limit_memory(memory_mb)
        install_audit_hook(runtime_roots())
        self.armed = True

    def learn(self, req: dict[str, Any]) -> None:
        source = req["source"]
        digest = hashlib.sha256(source.encode()).hexdigest()
        if digest != req["hash"]:
            raise ValueError("the source does not match its hash")
        self.sources[digest] = source

    def load(self, digest: str, file: str | None):
        """The script compiled under its file name (two files with the same text are two
        modules: tracebacks and the id name the right one)."""
        file = file or "script.py"
        key = (digest, file)
        if key not in self.modules:
            try:
                self.modules[key] = _compile(file, self.sources[digest])
            except BaseException as exc:
                self.modules[key] = exc
        got = self.modules[key]
        if isinstance(got, BaseException):
            raise got
        return got

    def compute(self, indicator, params: dict[str, Any], arrays: dict[str, Any], info: Any = None):
        import numpy as np
        import pandas as pd

        columns = ("ts", "o", "h", "l", "c", "v")
        missing = [c for c in columns if c not in arrays]
        if missing:
            raise ValueError(f"bars without {missing}")
        n = len(arrays["ts"])
        if any(len(arrays[c]) != n for c in columns):
            raise ValueError("bar columns of different lengths")
        bars = pd.DataFrame({c: np.array(arrays[c], dtype="float64") for c in columns})
        from openchart.indicators._base import Info

        info = Info.from_json(info) if info is not None else None
        t = time.perf_counter()
        series = indicator.run(bars, params, info)
        ms = (time.perf_counter() - t) * 1000
        out = {name: s.to_numpy(dtype="float64") for name, s in series.items()}
        return {"outputs": list(out), "compute_ms": round(ms, 3)}, out

    def describe_error(self, exc: BaseException, req: dict[str, Any]) -> dict[str, Any]:
        return script_error(exc, req.get("file"))


def _compile(file: str, source: str):
    """Run the script as a module of its own, never under openchart.indicators (that
    would replace the helpers scripts import), then check what it declares."""
    from openchart.indicators._base import MAX_FILE_BYTES, SchemaError
    from openchart.indicators._registry import from_module

    if len(source.encode()) > MAX_FILE_BYTES:
        raise SchemaError(f"{file}: the file is larger than {MAX_FILE_BYTES // 1024} KB")
    stem = file[:-3] if file.endswith(".py") else file
    module = types.ModuleType(f"openchart_custom.{stem}")
    module.__file__ = file
    code = compile(source, file, "exec", dont_inherit=True)
    exec(code, module.__dict__)
    return from_module(stem, module, builtin=False, file=file)


def _error_info(kind: str, message: str) -> dict[str, Any]:
    return {"type": kind, "message": message, "line": None, "traceback": ""}


def script_error(exc: BaseException, file: str | None) -> dict[str, Any]:
    """The error as the script's author needs it: its type and message, the line in the
    script, and a traceback cut down to the script's own frames."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == file]
    line = frames[-1].lineno if frames else None
    if isinstance(exc, SyntaxError) and exc.filename == file:
        line = exc.lineno
    lines = [
        f"  line {f.lineno}, in {f.name}" + (f"\n    {f.line}" if f.line else "") for f in frames
    ]
    if isinstance(exc, SyntaxError) and exc.filename == file:
        lines.append(f"  line {exc.lineno}" + (f"\n    {exc.text.rstrip()}" if exc.text else ""))
    message = str(exc) if not isinstance(exc, SyntaxError) else (exc.msg or str(exc))
    return {
        "type": type(exc).__name__,
        "message": message[:2000],
        "line": line,
        "traceback": "\n".join(lines)[:8000],
        "memory": isinstance(exc, MemoryError),
    }


# The audit hook (a courtesy, not a wall) -------------------------------------------------

_REFUSED = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "os.startfile",
    "os.kill",
    "os.killpg",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.chmod",
    "os.chown",
    "os.link",
    "os.symlink",
    "os.truncate",
    "os.utime",
    "os.putenv",
    "os.unsetenv",
    "shutil.",
    "ctypes.dlopen",
    "ctypes.dlsym",
    "ctypes.call_function",
    "ctypes.cdata",
    "winreg.",
    "_winapi.",
    "msvcrt.",
    "mmap.",
    "sqlite3.",
    "urllib.",
    "http.",
    "ftplib.",
    "smtplib.",
    "webbrowser.",
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def runtime_roots() -> list[str]:
    """The folders imports read from: the interpreter, its library, the site folders and
    this package (the frozen build: its own folder)."""
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable)}
    roots.update(p for p in sys.path if p)
    if getattr(sys, "_MEIPASS", None):
        roots.add(sys._MEIPASS)
    import openchart

    roots.add(os.path.dirname(os.path.dirname(os.path.abspath(openchart.__file__))))
    return sorted(os.path.normcase(os.path.abspath(r)) for r in roots if r)


def install_audit_hook(roots: list[str]) -> None:
    allowed = tuple(r.rstrip("\\/") + os.sep for r in roots)

    def readable(path: Any) -> bool:
        if isinstance(path, int):  # an already open descriptor (our own pipes)
            return True
        try:
            full = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return False
        return any(full.startswith(r) or full + os.sep == r for r in allowed)

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (*tuple(args), None, None, 0)[:3]
            writing = bool(mode and any(c in mode for c in "wax+")) or bool(
                (flags or 0) & _WRITE_FLAGS
            )
            if writing or not readable(path):
                raise PermissionError("custom indicators cannot open files")
        elif event in ("os.listdir", "os.scandir"):
            if not args or not readable(args[0] if args[0] is not None else "."):
                raise PermissionError("custom indicators cannot list folders")
        elif event.startswith(_REFUSED):
            what = event.split(".", 1)[0]
            raise PermissionError(f"custom indicators cannot use {what} ({event})")

    sys.addaudithook(hook)


if __name__ == "__main__":
    sys.exit(main())
