"""The indicator worker on Linux and macOS: the Docker sidecar, and the "process only"
fallback.

**The sidecar** (`docker-compose.yml`) is a container of its own: another uid, no network,
no data volume, a read-only root, its own limits. It listens on a Unix socket in a small
shared volume. Each request runs in a child forked for it, with `RLIMIT_AS` set to the
child's current size plus the budget (numpy and pandas reserve a lot of address space,
CI-0 Docker finding) and `RLIMIT_CPU`, and is killed at its deadline. A hang or a memory
blow-up only takes that child; the container's limits are the outer wall.

**Process only** (one container without compose, or a source install on Linux or macOS):
a child process of the server, as the same user, in a clean environment. It is below the
minimum isolation: it could read the data folder and the server's environment.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from openchart.indicators.worker.protocol import ProtocolError, encode, exact_reader, read_frame

CPU_SECONDS = 60  # per forked child: a backstop behind the deadline


def vm_size() -> int:
    """This process's address space in bytes (Linux); 0 where /proc is missing."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def limit_memory(budget_mb: int) -> None:
    """Allocations beyond the current size plus the budget fail with MemoryError."""
    import resource

    size = vm_size()
    if not size:  # macOS: RLIMIT_AS is not enforced anyway
        return
    limit = size + budget_mb * 2**20
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


# Process only ---------------------------------------------------------------------------


@dataclass
class PosixProcess:
    popen: subprocess.Popen

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def stdin(self):
        return self.popen.stdin

    @property
    def stdout(self):
        return self.popen.stdout

    @property
    def stderr(self):
        return self.popen.stderr

    def alive(self) -> bool:
        return self.popen.poll() is None

    def exit_code(self) -> int | None:
        return self.popen.poll()

    def wait(self, ms: int) -> bool:
        try:
            self.popen.wait(ms / 1000)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            self.popen.kill()

    def close(self) -> None:
        self.kill()
        for f in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            with contextlib.suppress(OSError):
                f.close()
        with contextlib.suppress(Exception):
            self.popen.wait(5)


def start_process(argv: list[str], env: dict[str, str], cwd: str) -> PosixProcess:
    def lower_priority() -> None:  # runs in the child, before exec
        with contextlib.suppress(OSError):
            os.nice(10)

    popen = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        close_fds=True,
        start_new_session=True,
        preexec_fn=lower_priority,
    )
    return PosixProcess(popen)


# The sidecar, server side ----------------------------------------------------------------


class SocketProcess:
    """The server's connection to the sidecar, shaped like a worker process: killing it
    closes the connection (the sidecar kills a child still running at its deadline)."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(None)
        self.file = self.sock.makefile("rwb", buffering=0)
        self.stdin = self.stdout = self.file
        self.stderr = None
        self.pid = None
        self._open = True

    def alive(self) -> bool:
        return self._open

    def exit_code(self) -> int | None:
        return None

    def wait(self, ms: int) -> bool:
        return not self._open

    def kill(self) -> None:
        self.close()

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.file.close()
        with contextlib.suppress(OSError):
            self.sock.close()


# The sidecar itself ---------------------------------------------------------------------


def serve_socket(path: str, worker: Any) -> int:
    """`openchart indicator-worker --listen <socket>`: each connection (the server's, an
    `openchart sandbox check`) in a forked process, each request in a child of that."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o666)  # the server runs as another uid
    srv.listen(4)
    print(f"indicator worker listening on {path} as uid {os.getuid()}", flush=True)
    while True:
        conn, _ = srv.accept()
        with contextlib.suppress(ChildProcessError):  # connections that ended
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        if os.fork() == 0:
            srv.close()
            try:
                _serve(conn, worker)
            except (OSError, EOFError, ProtocolError) as exc:
                print(f"indicator worker: connection ended: {exc}", flush=True)
            finally:
                os._exit(0)
        conn.close()


def _serve(conn: socket.socket, worker: Any) -> None:
    f = conn.makefile("rwb", buffering=0)
    read = exact_reader(f)
    f.write(encode(worker.hello()))
    budget_mb = 512
    while True:
        try:
            req, arrays = read_frame(read)
        except EOFError:
            return
        cmd = req.get("cmd")
        if cmd == "exit":
            return
        if cmd == "arm":  # each child arms itself; remember the budget
            budget_mb = int(req.get("memory_mb") or budget_mb)
            _answer(f, req, {"armed": True})
            continue
        if cmd in ("describe", "warmup", "compute") and req.get("source") is not None:
            try:
                worker.learn(req)
            except (KeyError, ValueError) as exc:
                _answer(f, req, {"error": _error("ValueError", str(exc))})
                continue
        _run_in_child(f, worker, req, arrays, budget_mb)


def _run_in_child(f, worker: Any, req: dict, arrays: dict, budget_mb: int) -> None:
    deadline = float(req.get("deadline_s") or 5.0)
    start = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # the child: limits, the audit hook, the work, the answer, gone
        code = 0
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
            if req.get("cmd") != "probe":
                worker.arm(budget_mb)
            head, out = worker.handle(req, arrays)
            f.write(encode(head, out))
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    status = None
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.perf_counter() - start > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            _answer(f, req, {"error": _error("Timeout", f"timed out after {deadline:g} s")})
            return
        time.sleep(0.002)
    if os.WIFSIGNALED(status) or os.WEXITSTATUS(status):
        how = (
            f"signal {os.WTERMSIG(status)}"
            if os.WIFSIGNALED(status)
            else f"exit code {os.WEXITSTATUS(status)}"
        )
        memory = os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        error = {**_error("WorkerCrash", f"the worker ended ({how})"), "memory": memory}
        _answer(f, req, {"error": error})


def _answer(f, req: dict, head: dict[str, Any]) -> None:
    """An answer the sidecar writes itself (not `Worker.handle`'s), with the request's id
    (S6): the runner refuses any answer that does not echo it."""
    if "rid" in req:
        head = {**head, "rid": req["rid"]}
    f.write(encode(head))


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": kind, "message": message, "line": None, "traceback": ""}


if __name__ == "__main__":
    sys.exit("run: openchart indicator-worker --listen <socket>")
