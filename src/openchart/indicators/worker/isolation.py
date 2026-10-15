"""Which worker this install gets, and how isolated it really is (docs/ci1-plan.md 1).

| Install | Level | Minimum isolation |
|---|---|---|
| Windows service, release build, set up by the installer | `appcontainer` | met |
| Windows service, source checkout or no sandbox | `unavailable` | no custom code (D7 a) |
| Source install run by its user (`openchart run`) | `restricted` | not met |
| Release build run by its user (not the service) | `restricted` (kept at its integrity) | not met |
| Docker with compose | `sidecar` | met |
| Docker without compose, Linux, macOS | `process-only` | not met |

The level is decided once at start: a worker is started and probed (no secrets in its
environment, the data folder, database and key file closed, no network to 127.0.0.1, the
server process closed) before it is armed. Below the minimum, browser editing is off
whatever the setting says, and the Custom section shows a notice; files in the indicators
folder still run, in that weaker worker.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import sys
import sysconfig
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openchart.indicators.worker.runner import IndicatorRunner, Limits, WorkerUnavailable

log = logging.getLogger("openchart.indicators.worker")

SANDBOX_SID_ENV = "OPENCHART_SANDBOX_SID"
SOCKET_ENV = "OPENCHART_WORKER_SOCKET"
MINIMUM_LEVELS = ("appcontainer", "sidecar")
# The one failure of the restricted worker after which the plain job-limited worker may
# run (owner's decision A1): it died at start without a usable desktop (CI runners).
RESTRICTED_FALLBACK = "exit code 0xc0000142 STATUS_DLL_INIT_FAILED"
WEAKER_NOTICE = (
    "weaker isolation: this install cannot isolate custom code from your data and tokens"
)
THREAD_VARS = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def limits_from_env(env: dict[str, str] | None = None) -> Limits:
    env = os.environ if env is None else env

    def number(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(env.get(name, default))
        except ValueError:
            log.warning("%s is not a number; using %g", name, default)
            return default
        return min(max(value, low), high)

    return Limits(
        memory_mb=int(number("OPENCHART_WORKER_MEMORY_MB", 512, 128, 8192)),
        cpu_percent=number("OPENCHART_WORKER_CPU_PERCENT", 50, 5, 100),
        timeout_s=number("OPENCHART_WORKER_TIMEOUT_S", 5, 1, 30),
        folder_mb=int(number("OPENCHART_WORKER_FOLDER_MB", 50, 1, 1024)),
    )


@dataclass
class Isolation:
    level: str  # appcontainer | sidecar | restricted | process-only | unavailable
    minimum_met: bool
    reason: str
    probes: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def runs_custom_code(self) -> bool:
        return self.level != "unavailable"

    @property
    def notice(self) -> str | None:
        if self.level == "unavailable":
            return f"custom indicators do not run on this install: {self.reason}"
        return None if self.minimum_met else WEAKER_NOTICE

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "minimum_met": self.minimum_met,
            "reason": self.reason,
            "notice": self.notice,
            "probes": {k: v.get("result") for k, v in self.probes.items()},
            "notes": self.notes,
        }


# Command lines and environments ---------------------------------------------------------


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def package_root() -> str:
    """The folder holding the openchart package this server runs (a worktree's `src`,
    or site-packages): the worker imports the same code."""
    import openchart

    return str(Path(openchart.__file__).resolve().parent.parent)


def site_folders() -> list[str]:
    """The folders this interpreter's packages come from (a venv's site-packages, the
    user's): the worker runs the base interpreter in isolated mode and adds them."""
    out: list[str] = []
    for p in [sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"], *sys.path]:
        site = p and Path(p).is_dir() and Path(p).name in ("site-packages", "dist-packages")
        if site and p not in out:
            out.append(p)
    return out


def base_python() -> str:
    """A venv's python.exe on Windows is a launcher that starts the base interpreter as a
    child, which the job's one-process limit refuses (finding 3)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def worker_argv(*, parent: int | None = None) -> tuple[list[str], str]:
    """The worker's command line and working folder: the frozen build starts itself; a
    source install starts the base interpreter on child.py with the package and site
    folders. No path to the data folder, no secrets."""
    if frozen():
        exe = sys.executable
        argv = [exe, "indicator-worker"]
        return argv, str(Path(exe).parent)
    from openchart.indicators.worker import child

    python = base_python()
    argv = [python, "-I", "-B", "-X", "utf8", str(Path(child.__file__).resolve())]
    argv += ["--path", package_root()]
    for folder in site_folders():
        argv += ["--site", folder]
    if parent:
        argv += ["--parent", str(parent)]
    return argv, str(Path(python).parent)


def clean_env(runtime_dir: str, *, appcontainer: bool = False) -> dict[str, str]:
    """An environment built from nothing: no OPENCHART_* (the secret key), no tokens."""
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env = {"SYSTEMROOT": root, "PATH": f"{runtime_dir};{root}\\System32", **THREAD_VARS}
        if appcontainer:
            # CreateProcess fails with error 203 without it; Windows maps it, TEMP and TMP
            # to the container's own folder (finding 1).
            env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA") or os.path.expandvars(
                r"%SystemRoot%\System32\config\systemprofile\AppData\Local"
            )
        return env
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **THREAD_VARS}


# Launchers ------------------------------------------------------------------------------


class WindowsLauncher:
    enforces_deadline = False

    def __init__(
        self,
        identity: str,
        limits: Limits,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        private_desktop: bool | None = None,
        low_integrity: bool = True,
        container_name: str | None = None,
    ):
        """`private_desktop`: None decides it: a sandboxed identity started without an
        interactive window station (a service, a CI runner) gets a desktop of its own
        (finding 7; found again on GitHub's Windows runner in CI-1)."""
        from openchart.indicators.worker import windows

        self.identity = identity
        self.level = {"appcontainer": "appcontainer", "restricted": "restricted"}.get(
            identity, "process-only"
        )
        self.limits = limits
        default_argv, default_cwd = worker_argv()
        self.argv, self.cwd = argv or default_argv, cwd or default_cwd
        if private_desktop is None:
            private_desktop = identity != "plain" and not windows.interactive_window_station()
        self.private_desktop = private_desktop
        self.low_integrity = low_integrity
        self.container_name = container_name or windows.CONTAINER_NAME
        self.profile_writes = "unchanged"
        self.desktop_name: str | None = None
        self._sid = None

    def container(self):
        from openchart.indicators.worker import windows

        if self._sid is None:
            self._sid = windows.ensure_profile(self.container_name)
        return self._sid

    def start(self):
        from openchart.indicators.worker import windows

        env = clean_env(
            str(Path(self.argv[0]).parent), appcontainer=self.identity == "appcontainer"
        )
        if self.identity == "appcontainer" and self.profile_writes == "emptied":
            self.empty_profile()
        handle = windows.start(
            self.argv,
            identity=self.identity,
            env=env,
            cwd=self.cwd,
            container=self.container() if self.identity == "appcontainer" else None,
            memory_mb=self.limits.memory_mb,
            cpu_percent_of_one_core=self.limits.cpu_percent,
            private_desktop=self.private_desktop,
            low_integrity=self.low_integrity,
        )
        self.desktop_name = handle.desktop[2] if handle.desktop else None
        return handle

    def sid_text(self) -> str:
        from openchart.indicators.worker import windows

        return windows.sid_to_string(self.container())

    def profile_folder(self) -> str | None:
        from openchart.indicators.worker import windows

        return windows.profile_folder(self.sid_text())

    def own_folder(self) -> str | None:
        """The folder the runner watches (B1): the AppContainer's own, when it is
        writable."""
        if self.identity != "appcontainer" or self.profile_writes != "emptied":
            return None
        return self.profile_folder()

    def empty_profile(self) -> None:
        folder = self.profile_folder()
        if not folder or not Path(folder).is_dir():
            return
        for child in Path(folder).iterdir():
            with contextlib.suppress(OSError):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()

    def describe_exit(self, code: int | None) -> str:
        from openchart.indicators.worker import windows

        return "still running" if code is None else windows.describe_exit_code(code)


class ProcessLauncher:
    """Linux and macOS without the sidecar: a child of the server, same user."""

    level = "process-only"
    enforces_deadline = False

    def __init__(self, limits: Limits, *, argv: list[str] | None = None, cwd: str | None = None):
        self.limits = limits
        default_argv, default_cwd = worker_argv(parent=os.getpid())
        self.argv, self.cwd = argv or default_argv, cwd or default_cwd

    def start(self):
        from openchart.indicators.worker.posix import start_process

        return start_process(self.argv, clean_env(""), self.cwd)

    def describe_exit(self, code: int | None) -> str:
        if code is None:
            return "still running"
        return f"signal {-code}" if code < 0 else f"exit code {code}"


class SidecarLauncher:
    """Docker with compose: the worker container's Unix socket."""

    level = "sidecar"
    enforces_deadline = True

    def __init__(self, path: str):
        self.path = path

    def start(self):
        from openchart.indicators.worker.posix import SocketProcess

        return SocketProcess(self.path)

    def describe_exit(self, code: int | None) -> str:
        return "the connection to the sidecar closed"


# Deciding -------------------------------------------------------------------------------


@dataclass
class Plan:
    launchers: list[Any]  # tried in order
    unavailable: str | None = None  # why nothing can run (D7 a), when launchers is empty


def plan(limits: Limits) -> Plan:
    if os.name == "nt":
        from openchart.indicators.worker import windows

        # A service install: LocalSystem, or one the installer recorded a sandbox for. It
        # gets the AppContainer or no custom code at all (D7 a); never a fallback.
        if windows.running_as_system() or os.environ.get(SANDBOX_SID_ENV, "").strip():
            if not frozen():
                return Plan(
                    [],
                    "custom indicators need the release build; with a source "
                    "checkout they do not run under the service",
                )
            recorded = os.environ.get(SANDBOX_SID_ENV, "").strip()
            if not recorded:
                return Plan(
                    [],
                    "the indicator sandbox is not set up: rerun the installer "
                    "(install-service.cmd)",
                )
            if recorded != windows.container_sid():
                return Plan(
                    [], f"{SANDBOX_SID_ENV} does not match the sandbox: rerun the installer"
                )
            return Plan([WindowsLauncher("appcontainer", limits, private_desktop=True)])
        # A source install, or a release zip run by hand: Low integrity. A frozen build
        # cannot start at Low outside an AppContainer (finding 9), so it keeps the server's
        # level. The plain job-limited worker follows only when the restricted one was seen
        # to die with STATUS_DLL_INIT_FAILED (owner's decision A1, start_worker): GitHub's
        # runner, where no property of the session predicts it (CI-1).
        return Plan(
            [
                WindowsLauncher("restricted", limits, low_integrity=not frozen()),
                WindowsLauncher("plain", limits),
            ]
        )
    path = os.environ.get(SOCKET_ENV, "").strip()
    if path:
        return Plan([SidecarLauncher(path)])
    return Plan([ProcessLauncher(limits)])


def probe_targets(data_dir: Path, port: int, level: str) -> dict[str, Any]:
    """What the worker must not reach. Reading probes take a few bytes and keep none; the
    only writes are into the worker's own container folder. Absolute paths: the worker's
    working folder is not the server's (found in CI-1)."""
    data_dir = Path(data_dir).resolve()
    read = {"read the database": str(data_dir / "openchart.db")}
    if (data_dir / "secrets.enc").exists():
        read["read secrets.enc"] = str(data_dir / "secrets.enc")
    key_file = os.environ.get("OPENCHART_SECRET_KEY_FILE", "").strip()
    if key_file:
        read["read the key file"] = key_file
    targets: dict[str, Any] = {
        "read": read,
        "list": {"list the data folder": str(data_dir)},
        # The sidecar has a PID namespace of its own: the server's pid means nothing there.
        "parent_pid": os.getpid() if level != "sidecar" else None,
        "connect": {"connect to 127.0.0.1": ["127.0.0.1", port, 2.0]},
        "program": True,
    }
    if level in MINIMUM_LEVELS:
        # A reserved documentation address: one connection attempt, refused locally.
        targets["connect"]["connect out (192.0.2.1:9)"] = ["192.0.2.1", 9, 2.0]
    return targets


REQUIRED = (
    "read the database",
    "read secrets.enc",
    "read the key file",
    "list the data folder",
    "open the server process",
    "connect to 127.0.0.1",
    "connect out (192.0.2.1:9)",
)


def judge(level: str, probes: dict[str, Any]) -> tuple[bool, str]:
    """Whether the probes show the minimum, and the first thing that breaks it."""
    if probes.get("environment", {}).get("result") != "clean":
        return False, f"secrets in the worker's environment ({probes.get('environment')})"
    if level not in MINIMUM_LEVELS:
        return False, f"{level}: below the minimum isolation"
    for name in REQUIRED:
        if probes.get(name, {}).get("result") == "allowed":
            return False, f"the worker could {name[0].lower() + name[1:]}"
    if level == "appcontainer" and probes.get("start a program", {}).get("result") == "allowed":
        return False, "the worker could start a program"
    return True, "minimum isolation met"


class _Listener:
    """A port on 127.0.0.1 nobody uses, for the loopback probe."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.5)
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                continue

    def close(self) -> None:
        self._stop = True
        self.sock.close()


async def start_worker(
    data_dir: Path, limits: Limits | None = None, launchers: list[Any] | None = None
) -> tuple[IndicatorRunner | None, Isolation]:
    """Start the worker this install gets, probe it, arm it. Never raises: when nothing
    can run, the runner is None and the isolation says why."""
    limits = limits or limits_from_env()
    if launchers is None:
        try:
            decided = plan(limits)
        except Exception as exc:
            return None, Isolation("unavailable", False, f"{type(exc).__name__}: {exc}")
        if not decided.launchers:
            return None, Isolation("unavailable", False, decided.unavailable or "no worker")
        launchers = decided.launchers
    failures: list[str] = []
    for launcher in launchers:
        runner, isolation = await _try(launcher, data_dir, limits)
        if runner is not None:
            isolation.notes[:0] = failures
            if failures:
                log.warning("indicator sandbox fallback: %s", "; ".join(failures))
            return runner, isolation
        failures.append(f"{launcher.level}: {isolation.reason}")
        if launcher.level in MINIMUM_LEVELS:  # D7 (a): never fall back below it
            break
        if launcher.level == "restricted" and RESTRICTED_FALLBACK not in isolation.reason:
            break  # A1: only that observed failure leads to the plain worker
    return None, Isolation("unavailable", False, "; ".join(failures) or "no worker")


async def _try(
    launcher: Any, data_dir: Path, limits: Limits
) -> tuple[IndicatorRunner | None, Isolation]:
    notes: list[str] = []
    if getattr(launcher, "identity", None) == "appcontainer":
        notes.append(_restrict_profile(launcher))
    runner = IndicatorRunner(launcher, limits)
    listener = _Listener()
    try:
        targets = probe_targets(data_dir, listener.port, launcher.level)
        if getattr(launcher, "identity", None) == "appcontainer" and (
            folder := launcher.profile_folder()
        ):
            targets["write"] = {"write into its own folder": folder}
        try:
            started = await runner.start(targets)
        except (WorkerUnavailable, OSError) as exc:
            if launcher.level == "appcontainer" and launcher.profile_writes == "denied":
                # Finding 2: if it cannot start without writing its folder, let it write
                # there and empty the folder at each start instead.
                _allow_profile(launcher)
                notes.append("its folder is writable again, and emptied at each start")
                runner = IndicatorRunner(launcher, limits)
                try:
                    started = await runner.start(targets)
                except (WorkerUnavailable, OSError) as exc2:
                    return None, Isolation("unavailable", False, str(exc2), notes=notes)
            else:
                return None, Isolation("unavailable", False, str(exc), notes=notes)
    finally:
        listener.close()
    probes = started["probes"] or {}
    if probes.get("write into its own folder", {}).get("result") == "allowed":
        # The removal of that write did not hold (CI-1): say so, and from now on empty the
        # folder before each start and cap it while the worker runs (owner's decision B1).
        launcher.profile_writes = "emptied"
        notes[:] = [n for n in notes if n != "writes to its own folder removed"]
        notes.append(
            f"its own folder is writable: emptied at each worker start and capped at "
            f"{limits.folder_mb} MB while it runs"
        )
        await runner.stop()  # this worker started before the watch: start a watched one
        runner = IndicatorRunner(launcher, limits)
        try:
            await runner.start()
        except (WorkerUnavailable, OSError) as exc:
            return None, Isolation("unavailable", False, str(exc), probes, notes)
    met, why = judge(launcher.level, probes)
    if launcher.level in MINIMUM_LEVELS and not met:
        await runner.stop()
        return None, Isolation("unavailable", False, why, probes, notes)
    if probes.get("environment", {}).get("result") != "clean":
        await runner.stop()
        return None, Isolation("unavailable", False, why, probes, notes)
    desktop = getattr(launcher, "desktop_name", None)
    if desktop:
        notes.append(f"a private desktop of its own ({desktop})")
    isolation = Isolation(launcher.level, met, why, probes, notes)
    log.info(
        "indicator sandbox probes: %s",
        ", ".join(f"{k} {v.get('result')}" for k, v in probes.items() if v.get("result") != "info"),
    )
    if notes:
        log.info("indicator sandbox notes: %s", "; ".join(notes))
    log.info(
        "indicator sandbox: ready (%s)%s",
        _LEVEL_NAMES.get(launcher.level, launcher.level),
        "" if met else f"; {WEAKER_NOTICE}",
    )
    return runner, isolation


_LEVEL_NAMES = {
    "appcontainer": "AppContainer",
    "sidecar": "sidecar",
    "restricted": "restricted token",
    "process-only": "process only",
}


def _restrict_profile(launcher: WindowsLauncher) -> str:
    try:
        folder = launcher.profile_folder()
        if not folder:
            launcher.profile_writes = "unchanged"
            return "the container has no folder of its own yet"
        from openchart.indicators.worker import windows

        windows.deny_profile_writes(folder, launcher.sid_text())
        launcher.profile_writes = "denied"
        return "writes to its own folder removed"
    except OSError as exc:
        launcher.profile_writes = "emptied"
        return f"could not remove writes to its own folder ({exc}); emptied at each start"


def _allow_profile(launcher: WindowsLauncher) -> None:
    from openchart.indicators.worker import windows

    folder = launcher.profile_folder()
    if folder:
        windows.allow_profile_writes(folder, launcher.sid_text())
    launcher.profile_writes = "emptied"
