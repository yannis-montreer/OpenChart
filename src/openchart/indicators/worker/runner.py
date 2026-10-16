"""`IndicatorRunner`: the server's side of the indicator worker.

- **One worker** by default, started in the background; requests queue for it and run one
  at a time. Pipes are read and written in threads, so the event loop (feeds, candles,
  alerts, the watchdog) never waits on the worker.
- **Two priorities:** a save-time check's job (`priority="low"`) starts only when no job
  runs and no chart request waits, so a chart waits for at most one check job.
- **Start priority** (Windows): a worker is created at normal priority, so that it can
  import numpy and pandas when every core is busy, and the server lowers it to below
  normal as soon as its hello arrives, before anything is sent to it. The priority is read
  back; if it is not below normal, the worker is killed and the indicators are unavailable
  with that reason (fail closed). A respawn takes the same path.
- **A deadline per job** (5 s by default, 1 to 30 s). When it passes, the worker is killed,
  that request answers "timed out after 5 s", and a new worker starts.
- **Respawn:** a worker that dies or is killed is replaced. A queued job that finds the
  worker gone before it could send its request is retried once on the new one.
- **Scripts are sent as source text**, by hash: the worker caches what it has compiled, so
  the source travels again only after a respawn (the worker answers "NeedSource").

Quarantine (3 failures in a row) is the registry's: it knows when a file changes.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from openchart.indicators._base import MAX_OUTPUTS
from openchart.indicators.worker.protocol import ProtocolError, encode, exact_reader, read_frame

log = logging.getLogger("openchart.indicators.worker")

# Seconds for a new worker to say hello. The import takes 1 to 3 s on an idle machine and
# about 7 s next to as many busy loops as cores (docs/investigations/worker-start-under-load.md).
START_TIMEOUT = 90.0
BELOW_NORMAL = "below_normal"
STDERR_LINES_PER_MINUTE = 30
RETRY_AFTER_FAILED_START = 10.0  # seconds before trying to start a worker again
FOLDER_POLL = 0.5  # seconds between two measurements of a worker's own folder (B1)
NEED_SOURCE = "NeedSource"


@dataclass(frozen=True)
class Limits:
    memory_mb: int = 512
    cpu_percent: float = 50.0  # of one core
    timeout_s: float = 5.0
    folder_mb: int = 50  # what a worker may write into its own folder, where it can (B1)


class Handle(Protocol):
    """A running worker as a launcher returns it (`windows.WinProcess`, `posix.*`)."""

    pid: int | None
    stdin: Any
    stdout: Any
    stderr: Any

    def alive(self) -> bool: ...
    def exit_code(self) -> int | None: ...
    def wait(self, ms: int) -> bool: ...
    def kill(self) -> None: ...
    def close(self) -> None: ...

    # Optional (the Windows handle): `lower_priority()` lowers the process to below normal,
    # `priority()` names its class now, `cpu_seconds()` is the CPU time it has used.


class Launcher(Protocol):
    level: str  # appcontainer | sidecar | restricted | process-only
    # True when the worker is created at normal priority and must be lowered by the
    # runner after its hello (Windows). A handle that cannot do it is a failed start.
    lowers_priority_after_hello: bool
    # The sidecar kills its own children at the deadline; the runner then waits a little
    # longer before it gives up on the connection.
    enforces_deadline: bool

    def start(self) -> Handle: ...  # blocking
    def describe_exit(self, code: int | None) -> str: ...


class WorkerUnavailable(RuntimeError):
    """No worker can run here, or the last one could not be started."""


class ScriptFailed(Exception):
    """A request the script did not complete. `kind`: error (its own exception), timeout,
    memory, crash."""

    def __init__(self, kind: str, info: Mapping[str, Any]):
        self.kind = kind
        self.info = dict(info)
        super().__init__(self.summary)

    @property
    def summary(self) -> str:
        message = self.info.get("message") or ""
        if self.kind in ("timeout", "crash"):
            return message
        if self.kind == "memory":
            return message or "ran out of memory"
        line = self.info.get("line")
        text = (
            f"{self.info.get('type', 'Error')}: {message}"
            if message
            else str(self.info.get("type"))
        )
        return f"{text} (line {line})" if line else text

    @property
    def counts_for_quarantine(self) -> bool:
        return self.kind in ("timeout", "memory", "crash")


class _Died(Exception):
    def __init__(self, sent: bool):
        self.sent = sent


_EOF = object()


@dataclass
class _Live:
    handle: Handle
    frames: asyncio.Queue
    hello: dict[str, Any]
    known: set[str] = field(default_factory=set)  # script hashes it has the source of
    started: float = field(default_factory=time.monotonic)
    over_folder: str | None = None  # why the folder watch killed it


class IndicatorRunner:
    def __init__(
        self,
        launcher: Launcher,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.launcher = launcher
        self.clock = clock  # for ready_at and last_job_s (tests give a fake one)
        self.limits = limits or Limits()
        self.level = launcher.level
        self.state = "stopped"  # starting | ready | restarting | failed | stopped
        self.last_error: str | None = None
        self.restarts = 0
        self.start_seconds: float | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self.ready_at = -1e9  # monotonic time the current worker became ready
        self.last_job_s = 0.0  # the last job's exchange with the worker (no queue, no start)
        self._live: _Live | None = None
        # The queue: one job at a time. A "low" job (a save-time check) starts only when no
        # job runs and no "normal" one waits, so a chart waits for at most one check job.
        self._turn = asyncio.Condition()
        self._busy = False
        self._normal_waiting = 0
        self._spawning: asyncio.Task[_Live] | None = None
        self._first: asyncio.Future[None] | None = None
        self._failed_at = -1e9
        self._log_budget = (0.0, 0)

    # Life cycle ---------------------------------------------------------------------

    async def start(self, probe_targets: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Start the first worker. With `probe_targets`, it is probed before it is armed
        (isolation.py); returns {"hello": ..., "probes": ...}."""
        self.state = "starting"
        first = asyncio.get_running_loop().create_future()
        self._first = first
        live = None
        try:
            live = await self._launch()
            probes = None
            if probe_targets is not None:
                got, _ = await self._ask(
                    live, {"cmd": "probe", "targets": dict(probe_targets)}, {}, 30
                )
                probes = got.get("probes")
                if probes is None:
                    raise WorkerUnavailable(f"the worker did not answer the probes: {got}")
            await self._arm(live)
        except BaseException as exc:
            if live is not None:
                live.handle.kill()
                live.handle.close()
            self.state = "failed"
            self.last_error = str(exc) or type(exc).__name__
            self._failed_at = time.monotonic()
            first.set_result(None)
            raise
        self._live = live
        self.state = "ready"
        self.ready_at = self.clock()
        first.set_result(None)
        return {"hello": live.hello, "probes": probes}

    @property
    def up(self) -> bool:
        """A worker is running and ready (one that died between jobs is not, though no job
        has noticed yet)."""
        return self.state == "ready" and self._live is not None and self._live.handle.alive()

    def kill_for_tests(self) -> None:
        """Kill the current worker as a crash would (tests)."""
        if self._live is not None:
            self._live.handle.kill()

    async def stop(self) -> None:
        self.state = "stopped"
        if self._spawning is not None:
            self._spawning.cancel()
            with contextlib.suppress(BaseException):
                await self._spawning
            self._spawning = None
        live, self._live = self._live, None
        if live is not None:
            await asyncio.to_thread(self._shutdown, live)

    def _shutdown(self, live: _Live) -> None:
        with contextlib.suppress(Exception):
            live.handle.stdin.write(encode({"cmd": "exit"}))
            live.handle.stdin.flush()
            live.handle.wait(2000)
        live.handle.close()

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "level": self.level,
            "restarts": self.restarts,
            "error": self.last_error,
            "start_seconds": self.start_seconds,
            "pid": self._live.handle.pid if self._live else None,
        }

    # Requests -----------------------------------------------------------------------

    async def describe(self, digest: str, file: str, source: str) -> dict[str, Any]:
        head, _ = await self.request({"cmd": "describe"}, script=(digest, file, source))
        return head["definition"]

    async def warmup(self, digest: str, file: str, source: str, params: dict[str, Any]) -> int:
        head, _ = await self.request(
            {"cmd": "warmup", "params": params}, script=(digest, file, source)
        )
        return int(head["warmup"])

    async def compute(
        self,
        digest: str,
        file: str,
        source: str,
        params: dict[str, Any],
        bars: Mapping[str, np.ndarray],
        info: Mapping[str, Any] | None = None,
        *,
        priority: str = "normal",
    ) -> dict[str, np.ndarray]:
        header: dict[str, Any] = {"cmd": "compute", "params": params}
        if info is not None:
            header["info"] = dict(info)
        head, out = await self.request(
            header, bars, script=(digest, file, source), priority=priority
        )
        # The answer comes from code this server does not control: its list of outputs
        # must be a short list of names, each with an array, and nothing else.
        names = head.get("outputs")
        if (
            not isinstance(names, list)
            or len(names) > MAX_OUTPUTS
            or not all(isinstance(n, str) for n in names)
            or len(set(names)) != len(names)
            or any(n not in out for n in names)
        ):
            raise ScriptFailed(
                "error",
                {
                    "type": "ResultError",
                    "message": "the worker's answer does not list its outputs correctly",
                },
            )
        return dict(out)  # all of it: an array nobody declared is the caller's error to name

    async def request(
        self,
        header: dict[str, Any],
        arrays: Mapping[str, np.ndarray] | None = None,
        *,
        script: tuple[str, str, str] | None = None,
        priority: str = "normal",
    ) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        """One job: wait for the worker, send, answer within the deadline. Raises
        ScriptFailed or WorkerUnavailable. `priority="low"` waits until nothing else does."""
        if self.state == "stopped":
            raise WorkerUnavailable("the indicator worker is stopped")
        arrays = arrays or {}
        await self._take_turn(priority == "low")
        try:
            return await self._job(header, arrays, script)
        finally:
            async with self._turn:
                self._busy = False
                self._turn.notify_all()

    async def _take_turn(self, low: bool) -> None:
        async with self._turn:
            if low:
                await self._turn.wait_for(lambda: not self._busy and not self._normal_waiting)
            else:
                self._normal_waiting += 1
                try:
                    await self._turn.wait_for(lambda: not self._busy)
                finally:
                    self._normal_waiting -= 1
            self._busy = True

    async def _job(
        self,
        header: dict[str, Any],
        arrays: Mapping[str, np.ndarray],
        script: tuple[str, str, str] | None,
    ) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        timeout = self.limits.timeout_s
        retried = False
        while True:
            live = await self._ensure()
            head = dict(header, deadline_s=timeout)
            if script is not None:
                digest, file, source = script
                head.update(hash=digest, file=file)
                if digest not in live.known:
                    head["source"] = source
            wait = timeout + (2.0 if self.launcher.enforces_deadline else 0.0)
            t = self.clock()
            try:
                got, out = await self._ask(live, head, arrays, wait)
            except TimeoutError:
                self.last_job_s = self.clock() - t
                self._replace(live, f"a job ran past its {timeout:g} s deadline")
                raise ScriptFailed(
                    "timeout", {"type": "Timeout", "message": f"timed out after {timeout:g} s"}
                ) from None
            except _Died as died:
                self.last_job_s = self.clock() - t
                code = await asyncio.to_thread(self._exit_code, live)
                why = self.launcher.describe_exit(code)
                self._replace(live, f"the worker ended ({why})")
                if not died.sent and not retried:
                    retried = True  # it was gone before this job reached it
                    continue
                if live.over_folder:
                    raise ScriptFailed(
                        "crash", {"type": "FolderLimit", "message": live.over_folder}
                    ) from None
                memory = code is not None and (code & 0xFFFFFFFF) == 0xC0000017
                raise ScriptFailed(
                    "memory" if memory else "crash",
                    {"type": "WorkerCrash", "message": f"the worker ended ({why})"},
                ) from None
            self.last_job_s = self.clock() - t
            error = got.get("error")
            if error and error.get("type") == NEED_SOURCE and script is not None:
                live.known.discard(script[0])
                if not retried:
                    retried = True
                    continue
            if script is not None and "source" in head and not error:
                live.known.add(script[0])
            if error:
                raise ScriptFailed(self._kind(error), error)
            return got, out

    def _kind(self, error: Mapping[str, Any]) -> str:
        if error.get("type") == "Timeout":
            return "timeout"
        if error.get("memory") or error.get("type") == "MemoryError":
            return "memory"
        if error.get("type") == "WorkerCrash":
            return "crash"
        return "error"

    # Workers ------------------------------------------------------------------------

    async def _ensure(self) -> _Live:
        if self._first is not None and not self._first.done():  # the first start
            await asyncio.wait_for(asyncio.shield(self._first), START_TIMEOUT + 30)
        if self._live is not None and self._live.handle.alive():
            return self._live
        if self._live is not None:  # died between jobs
            self._replace(self._live, "the worker ended between jobs")
        if self._spawning is None and time.monotonic() - self._failed_at < RETRY_AFTER_FAILED_START:
            raise WorkerUnavailable(f"the indicator worker could not be started: {self.last_error}")
        if self._spawning is None:
            self._spawning = asyncio.create_task(self._respawn(), name="indicator-worker")
        try:
            return await asyncio.shield(self._spawning)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WorkerUnavailable(f"the indicator worker could not be started: {exc}") from exc

    def _replace(self, live: _Live, why: str) -> None:
        """Kill this worker; the next job starts another."""
        if self._live is live:
            self._live = None
        log.warning("indicator worker (pid %s) replaced: %s", live.handle.pid, why)
        live.handle.kill()
        threading.Thread(target=live.handle.close, daemon=True).start()
        if self.state != "stopped":
            self.state = "restarting"
            self.restarts += 1

    async def _respawn(self) -> _Live:
        try:
            live = await self._launch()
            await self._arm(live)
        except BaseException as exc:
            self.last_error = str(exc)
            self.state = "failed" if self.state != "stopped" else "stopped"
            self._failed_at = time.monotonic()
            self._spawning = None
            raise
        self._live = live
        self._spawning = None
        self.ready_at = self.clock()
        if self.state != "stopped":
            self.state = "ready"
        return live

    async def _arm(self, live: _Live) -> None:
        got, _ = await self._ask(live, {"cmd": "arm", "memory_mb": self.limits.memory_mb}, {}, 30)
        if not got.get("armed"):
            raise WorkerUnavailable(f"the worker did not arm: {got.get('error')}")

    async def _launch(self) -> _Live:
        loop = asyncio.get_running_loop()
        frames: asyncio.Queue = asyncio.Queue()
        t = time.perf_counter()
        handle = await asyncio.to_thread(self.launcher.start)
        live = _Live(handle, frames, {})
        try:
            return await self._handshake(live, loop, t)
        except BaseException:
            handle.kill()
            handle.close()
            raise

    async def _handshake(self, live: _Live, loop: asyncio.AbstractEventLoop, t: float) -> _Live:
        handle, frames = live.handle, live.frames
        threading.Thread(
            target=self._read_frames, args=(handle, frames, loop), daemon=True, name="worker-out"
        ).start()
        if handle.stderr is not None:
            threading.Thread(
                target=self._read_stderr, args=(handle,), daemon=True, name="worker-err"
            ).start()
        try:
            hello = await asyncio.wait_for(frames.get(), START_TIMEOUT)
        except TimeoutError:
            # CPU used tells starvation (little) from a hang (a lot).
            message = f"the worker did not start within {START_TIMEOUT:g} s{self._cpu_note(handle)}"
            log.warning("indicator worker (pid %s): %s", handle.pid, message)
            raise WorkerUnavailable(message) from None
        if hello is _EOF or not isinstance(hello, tuple) or not hello[0].get("ready"):
            code = await asyncio.to_thread(self._exit_code, live)
            await asyncio.sleep(0.05)  # let the stderr reader catch the last words
            tail = " | ".join(list(self.stderr_tail)[-3:])
            message = (
                f"the worker ended before it was ready: {self.launcher.describe_exit(code)}"
                + (f"; it said: {tail[:300]}" if tail else "")
                + self._cpu_note(handle)
            )
            log.warning("indicator worker (pid %s): %s", handle.pid, message)
            raise WorkerUnavailable(message)
        # Nothing has been sent to the worker yet. It runs at normal priority until now;
        # it is lowered before the probes, the arming or any script reach it.
        await asyncio.to_thread(self._lower_priority, handle)
        live.hello = hello[0]
        self.start_seconds = round(time.perf_counter() - t, 3)
        log.info(
            "indicator worker ready (%s, pid %s) in %.2f s",
            self.level,
            live.hello.get("pid"),
            self.start_seconds,
        )
        folder = getattr(self.launcher, "own_folder", lambda: None)()
        if folder:
            loop.create_task(self._watch_folder(live, folder), name="worker-folder")
        return live

    @staticmethod
    def _cpu_note(handle: Handle) -> str:
        cpu = getattr(handle, "cpu_seconds", lambda: None)
        try:
            used = cpu()
        except Exception:  # a note for the log must never hide the failure itself
            used = None
        return "" if used is None else f" (it had used {used:.2f} s of CPU)"

    def _lower_priority(self, handle: Handle) -> None:
        """Below normal, read back; anything else is a failed start (fail closed): the
        caller kills the worker, so none is left running at normal priority."""
        if not getattr(self.launcher, "lowers_priority_after_hello", False):
            return
        lower = getattr(handle, "lower_priority", None)
        read = getattr(handle, "priority", None)
        if lower is None or read is None:
            raise WorkerUnavailable("the worker's priority cannot be lowered: no way to set it")
        try:
            lower()
            got = read()
        except OSError as exc:
            raise WorkerUnavailable(f"could not lower the worker's priority: {exc}") from exc
        if got != BELOW_NORMAL:
            raise WorkerUnavailable(
                f"could not lower the worker's priority: it reads back as {got}, not below normal"
            )
        log.info("indicator worker (pid %s): priority lowered to below normal", handle.pid)

    async def _watch_folder(self, live: _Live, folder: str) -> None:
        """B1 (owner): where a worker can write into its own folder (the AppContainer's,
        when that write cannot be removed), the folder is emptied before each start, and
        while the worker lives it is measured every FOLDER_POLL seconds: past the limit,
        the worker is killed. A job running then fails as a crash ("wrote more than ...").
        Between two measurements it can write at disk speed: the limit is the budget plus
        what it writes in FOLDER_POLL seconds."""
        limit = self.limits.folder_mb * 2**20
        while live.handle.alive():
            try:
                size = await asyncio.to_thread(folder_size, folder)
            except OSError:
                size = 0
            if size > limit:
                live.over_folder = (
                    f"wrote {size / 2**20:.0f} MB into its own folder "
                    f"(limit {self.limits.folder_mb} MB)"
                )
                self._replace(live, live.over_folder)
                return
            await asyncio.sleep(FOLDER_POLL)

    async def _ask(
        self, live: _Live, header: dict[str, Any], arrays: Mapping[str, Any], wait: float
    ) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        data = encode(header, arrays)

        def write() -> None:
            live.handle.stdin.write(data)
            live.handle.stdin.flush()

        async def exchange():
            try:
                await asyncio.to_thread(write)
            except (OSError, ValueError):
                raise _Died(sent=False) from None
            got = await live.frames.get()
            if got is _EOF:
                raise _Died(sent=True)
            return got

        return await asyncio.wait_for(exchange(), wait)

    @staticmethod
    def _exit_code(live: _Live) -> int | None:
        live.handle.wait(2000)
        return live.handle.exit_code()

    @staticmethod
    def _read_frames(
        handle: Handle, frames: asyncio.Queue, loop: asyncio.AbstractEventLoop
    ) -> None:
        read = exact_reader(handle.stdout)
        try:
            while True:
                got = read_frame(read)
                loop.call_soon_threadsafe(frames.put_nowait, got)
        except (EOFError, OSError, ValueError, ProtocolError) as exc:
            if isinstance(exc, ProtocolError):
                log.error("indicator worker sent a bad frame: %s", exc)
                handle.kill()
        with contextlib.suppress(RuntimeError):  # the loop may be closed at shutdown
            loop.call_soon_threadsafe(frames.put_nowait, _EOF)

    def _read_stderr(self, handle: Handle) -> None:
        """What the worker (and print() in scripts) writes: kept for diagnostics and
        logged, at most STDERR_LINES_PER_MINUTE lines a minute."""
        with contextlib.suppress(OSError, ValueError):
            for raw in iter(handle.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()[:500]
                if not line:
                    continue
                self.stderr_tail.append(line)
                start, count = self._log_budget
                now = time.monotonic()
                if now - start > 60:
                    start, count = now, 0
                if count < STDERR_LINES_PER_MINUTE:
                    log.info("indicator worker: %s", line)
                elif count == STDERR_LINES_PER_MINUTE:
                    log.info("indicator worker: (more output this minute not logged)")
                self._log_budget = (start, count + 1)


def folder_size(folder: str) -> int:
    """Bytes in the folder and below; links and junctions are not followed."""
    total = 0
    for root, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total
