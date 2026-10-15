"""Workers for tests: the fake one (fast, no pandas) and the real one this machine's
user gets (restricted on Windows, a child process elsewhere)."""

import contextlib
import subprocess
from pathlib import Path

from openchart.indicators.worker import isolation
from openchart.indicators.worker.runner import Limits

HERE = Path(__file__).resolve().parent


class PopenHandle:
    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self.pid = popen.pid
        self.stdin, self.stdout, self.stderr = popen.stdin, popen.stdout, popen.stderr

    def alive(self):
        return self.popen.poll() is None

    def exit_code(self):
        return self.popen.poll()

    def wait(self, ms):
        try:
            self.popen.wait(ms / 1000)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self):
        with contextlib.suppress(OSError):
            self.popen.kill()

    def close(self):
        self.kill()
        for f in (self.stdin, self.stdout, self.stderr):
            with contextlib.suppress(OSError):
                f.close()
        with contextlib.suppress(Exception):
            self.popen.wait(5)


class FakeLauncher:
    """tests/fake_worker.py as a plain child process."""

    level = "process-only"
    enforces_deadline = False

    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1
        # The base interpreter: a venv's python.exe on Windows is a launcher whose child
        # would outlive a kill.
        argv = [isolation.base_python(), "-I", "-B", str(HERE / "fake_worker.py")]
        argv += ["--path", isolation.package_root()]
        for folder in isolation.site_folders():
            argv += ["--site", folder]
        popen = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return PopenHandle(popen)

    def describe_exit(self, code):
        return "still running" if code is None else f"exit code {code}"


def real_launchers(limits: Limits | None = None):
    """What `openchart run` would start for this user: restricted then plain on Windows,
    a child process on Linux and macOS."""
    limits = limits or Limits(timeout_s=5)
    decided = isolation.plan(limits)
    assert decided.launchers, decided.unavailable
    return decided.launchers
