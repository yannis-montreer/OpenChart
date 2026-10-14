"""CI-0 spike, Docker: the sidecar worker. Listens on a Unix socket; every request runs in
a child forked from this (numpy and pandas already imported), with rlimits and a deadline,
so a hang or a memory blow-up ends the child, not the worker.

    python docker_worker.py --listen /run/ocw/sock
"""

from __future__ import annotations

import os
import resource
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from worker_probe import recv, send, typical

# Per child: on top of the address space it inherits (numpy and pandas reserve a few
# hundred MB of it without using it), under the container's 512 MB.
AS_BUDGET = 256 * 2**20
# Not bare "KEY": the python image sets GPG_KEY, the public fingerprint of Python's
# release signing key.
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "OANDA", "TELEGRAM")


def vm_size() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    return 0


def attempt(fn) -> dict:
    try:
        return {"result": "allowed", "detail": fn()}
    except PermissionError as exc:
        return {"result": "denied", "detail": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        return {"result": "error", "detail": f"{type(exc).__name__}: {exc}"}


def read_file(path):
    with open(path, "rb") as f:
        return f"read {len(f.read())} bytes (content not shown)"


def write_file(folder):
    path = os.path.join(folder, "ci0-probe.tmp")
    with open(path, "wb") as f:
        f.write(b"x")
    os.remove(path)
    return f"wrote into {folder}"


def connect(host, port):
    s = socket.create_connection((host, port), timeout=3)
    s.close()
    return f"connected to {host}:{port}"


def many_processes(n=64):
    kids = []
    try:
        for _ in range(n):
            pid = os.fork()
            if pid == 0:
                time.sleep(5)
                os._exit(0)
            kids.append(pid)
        return f"started {n} processes"
    except OSError as exc:
        return f"stopped after {len(kids)} processes: {exc}"
    finally:
        for pid in kids:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def probes() -> dict:
    names = sorted(os.environ)
    secret = [n for n in names if any(w in n.upper() for w in SECRET_WORDS)]
    return {
        "identity": {"result": "info", "detail": f"uid {os.getuid()}, pid {os.getpid()}"},
        "environment": {
            "result": "secrets visible" if secret else "clean",
            "detail": ", ".join(names),
        },
        "read the database (/data/openchart.db)": attempt(lambda: read_file("/data/openchart.db")),
        "read /data/secrets.enc": attempt(lambda: read_file("/data/secrets.enc")),
        "processes visible in /proc": attempt(
            lambda: f"{sum(p.isdigit() for p in os.listdir('/proc'))} processes"
        ),
        "read pid 1's environment": attempt(lambda: read_file("/proc/1/environ")),
        "write into /": attempt(lambda: write_file("/")),
        "write into /tmp": attempt(lambda: write_file("/tmp")),
        "connect to the server container (server:8000)": attempt(lambda: connect("server", 8000)),
        "connect out (192.0.2.1:9)": attempt(lambda: connect("192.0.2.1", 9)),
        "start a program": attempt(
            lambda: f"sh ran, exit {subprocess.run(['sh', '-c', 'exit 7']).returncode}"
        ),
        "start 64 processes": attempt(many_processes),
    }


def child(conn_file, req, arrays, limit_as: bool) -> None:
    """Runs in the forked child; answers on the connection, then exits."""
    if limit_as:
        limit = vm_size() + AS_BUDGET
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    cmd = req["cmd"]
    if cmd == "probe":
        send(conn_file, {"probes": probes()})
    elif cmd == "compute":
        t = time.perf_counter()
        out = typical(arrays)
        send(conn_file, {"compute_ms": (time.perf_counter() - t) * 1000}, out)
    elif cmd == "hang":
        while True:
            pass
    elif cmd == "cpu":
        end = time.process_time() + req["seconds"]  # CPU seconds: the cap stretches wall time
        while time.process_time() < end:
            pass
        send(conn_file, {"done": True})
    elif cmd == "alloc":
        held, mb = [], 0
        try:
            while mb < req["mb"]:
                held.append(bytearray(64 * 2**20))
                mb += 64
            send(conn_file, {"alloc": f"allocated {mb} MB"})
        except MemoryError:
            del held
            send(conn_file, {"alloc": f"MemoryError after {mb} MB"})


def serve(conn: socket.socket) -> None:
    f = conn.makefile("rwb", buffering=0)
    while True:
        try:
            req, arrays = recv(f)
        except EOFError:
            return
        if req["cmd"] == "exit":
            return
        deadline = req.get("deadline", 5.0)
        t = time.perf_counter()
        pid = os.fork()
        if pid == 0:
            try:
                child(f, req, arrays, req.get("limit_as", True))
            finally:
                os._exit(0)
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.perf_counter() - t > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                send(
                    f,
                    {
                        "error": f"timed out after {deadline:g} s; child killed in "
                        f"{(time.perf_counter() - t - deadline) * 1000:.0f} ms"
                    },
                )
                status = None
                break
            time.sleep(0.005)
        if status is not None and (os.WIFSIGNALED(status) or os.WEXITSTATUS(status)):
            how = (
                f"signal {os.WTERMSIG(status)}"
                if os.WIFSIGNALED(status)
                else f"exit {os.WEXITSTATUS(status)}"
            )
            send(f, {"error": f"the child ended with {how}"})


def main() -> None:
    path = sys.argv[sys.argv.index("--listen") + 1]
    if os.path.exists(path):
        os.remove(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o666)
    srv.listen(1)
    print(f"worker listening on {path} as uid {os.getuid()}", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            serve(conn)


if __name__ == "__main__":
    main()
