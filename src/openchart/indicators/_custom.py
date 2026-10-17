"""Your own indicator files (`<data dir>/indicators/*.py`), as the server knows them.

The server never imports them, and does not even read their schema itself: it sends each
file's source to the worker, which answers what the file declares (`describe`) and
computes its values. Files are read again when their modification time or size changes,
so no restart is needed after editing one.

A definition that crashes the worker, times out or hits the memory limit 3 times in a
row is stopped ("quarantined") until its file changes, so one bad script cannot keep the
worker restarting for everyone. Its own exceptions are shown, not counted.

Each version of a file is checked for lookahead and a short warmup (`_checks.py`) on the
bars of its first chart requests, for at most CHECK_SETTINGS different settings: two more
worker jobs at the lowest priority, one check at a time, never right after the worker
starts. A check only ever warns: what it finds, or why it did not run, is in `checks`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from openchart.indicators import _checks
from openchart.indicators._base import (
    MAX_FILE_BYTES,
    Indicator,
    Info,
    Param,
    ResultError,
    output_from_json,
    validate_schema,
)
from openchart.indicators.worker.isolation import Isolation
from openchart.indicators.worker.runner import IndicatorRunner, ScriptFailed, WorkerUnavailable

log = logging.getLogger(__name__)

ID = re.compile(r"^[a-z][a-z0-9_]{0,39}\Z")  # \Z, not $: `$` also matches before a newline
QUARANTINE_AFTER = 3
WORKER_WAIT = 90.0  # seconds a request waits for the worker's first start
ERROR_CACHE_SIZE = 256  # a script's own errors kept, by script, parameters and bars
SLOW_ERROR_PAUSE = 30.0  # seconds a slow error is answered from memory
CHECK_SETTINGS = 5  # settings checked per version of a file (owner, CI-2 c)
CHECK_QUEUE = 16  # checks waiting at most; more wait for a later request
CHECK_STAGGER = 10.0  # seconds after a worker (re)start before a check runs
CHECK_GAP = 1.0  # seconds between two checks


def _bars_digest(bars: dict[str, np.ndarray]) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    for name in sorted(bars):
        h.update(name.encode())
        h.update(np.ascontiguousarray(bars[name], dtype="<f8").tobytes())
    return h.hexdigest()


def _not_here(*_args: Any) -> Any:
    raise RuntimeError("custom indicators are computed in the worker")


def indicator_from_json(indicator_id: str, d: dict[str, Any], file: str | None = None) -> Indicator:
    """The schema the worker described, for validating parameters here. It is checked by
    the same rules the worker applied to the file: the worker is not trusted, and what is
    built here is what the browser is told."""
    file = file or f"{indicator_id}.py"
    if not isinstance(d["params"], list) or not isinstance(d["outputs"], list):
        raise ValueError("params and outputs must be lists")
    params = []
    for p in d["params"]:
        params.append(
            Param(
                name=p["name"],
                type=p["type"],
                default=p["default"],
                label=p.get("label"),
                min=p.get("min"),
                max=p.get("max"),
                step=p.get("step"),
                choices=tuple(p.get("choices", ())) if p["type"] == "choice" else (),
                max_length=p.get("max_length") if p["type"] == "string" else None,
            )
        )
    outputs = [output_from_json(o) for o in d["outputs"]]
    validate_schema(file, d["name"], d["title"], params, outputs)
    return Indicator(
        id=indicator_id,
        name=d["name"],
        title=d["title"],
        overlay=bool(d["overlay"]),
        params=tuple(params),
        outputs=tuple(outputs),
        compute=_not_here,
        builtin=False,
    )


@dataclass
class CustomDefinition:
    id: str
    path: Path
    stamp: tuple[int, int]  # modification time (ns) and size: a change means a new version
    source: str | None = None
    digest: str | None = None
    status: str = "not_run"  # ok | error | quarantined | not_run
    reason: str | None = None
    line: int | None = None
    indicator: Indicator | None = None
    failures: int = 0  # crashes, timeouts and memory limits in a row
    error: dict[str, Any] | None = None  # the last problem: {type, message, line, traceback}
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)  # by settings, this version
    # Settings whose check had too few bars for a check run: a request with at least twice
    # as many bars checks them again (Session A's review: the first request after an edit
    # is often the browser's two-bar refresh).
    short: set[str] = field(default_factory=set)

    @property
    def file(self) -> str:
        return self.path.name

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": "custom",
            "file": self.file,
            "status": self.status,
            "reason": self.reason,
            "line": self.line,
            "definition": None,
            "checks": list(self.checks.values()),
        }
        if self.indicator is not None:
            out["definition"] = {**self.indicator.to_json(), "kind": "custom"}
        return out


class CustomIndicators:
    def __init__(self, folder: Path):
        self.folder = folder
        self.runner: IndicatorRunner | None = None
        self.isolation: Isolation | None = None  # None while the worker starts
        self._ready = asyncio.Event()
        self._defs: dict[str, CustomDefinition] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._scan = asyncio.Lock()
        # A script's own errors, by (hash, params, bars), and slow ones by (hash, params).
        self._errors: OrderedDict[tuple[str, str, str], ScriptFailed] = OrderedDict()
        self._slow: dict[tuple[str, str], tuple[float, ScriptFailed]] = {}
        self.check_stagger, self.check_gap = CHECK_STAGGER, CHECK_GAP
        self._pending: deque[_CheckJob] = deque()
        self._checker: asyncio.Task[None] | None = None
        self._last_check = -1e9
        # The check loop's pauses: on the runner's clock (`runner.clock`), slept with this
        # (tests give a fake clock and a sleep that moves it).
        self.sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
        self.check_log: deque[tuple[str, float, float]] = deque(maxlen=64)  # id, start, end

    def worker_ready(self, runner: IndicatorRunner | None, isolation: Isolation) -> None:
        self.runner, self.isolation = runner, isolation
        self._ready.set()

    async def _wait_worker(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), WORKER_WAIT)

    # The folder ---------------------------------------------------------------------

    def _files(self) -> dict[str, Path]:
        if not self.folder.is_dir():
            return {}
        return {p.stem: p for p in sorted(self.folder.glob("*.py")) if not p.stem.startswith("_")}

    async def refresh(self) -> list[CustomDefinition]:
        """Every file in the folder, each described by the worker when it changed."""
        async with self._scan:
            files = await asyncio.to_thread(self._files)
            for gone in set(self._defs) - set(files):
                del self._defs[gone]
            for stem, path in files.items():
                await self._update(stem, path)
            return [self._defs[s] for s in sorted(self._defs)]

    def file_lock(self, indicator_id: str) -> asyncio.Lock:
        """The lock every write of `<id>.py` holds (the editor's endpoints, delete), so a
        delete never runs in the middle of a save of the same file."""
        return self._file_locks.setdefault(indicator_id, asyncio.Lock())

    def forget(self, indicator_id: str) -> None:
        """Drop what is known of a file, so the next read describes it again: after a
        write through the API, whatever its modification time and size say."""
        self._defs.pop(indicator_id, None)

    async def get(self, indicator_id: str) -> CustomDefinition | None:
        """One definition, read again if its file changed."""
        path = self.folder / f"{indicator_id}.py"
        if not ID.match(indicator_id) or not await asyncio.to_thread(path.is_file):
            self._defs.pop(indicator_id, None)
            return None
        async with self._scan:
            await self._update(indicator_id, path)
            return self._defs.get(indicator_id)

    async def _update(self, stem: str, path: Path) -> None:
        try:
            st = await asyncio.to_thread(path.stat)
        except OSError:
            self._defs.pop(stem, None)
            return
        stamp = (st.st_mtime_ns, st.st_size)
        known = self._defs.get(stem)
        if known is not None and known.stamp == stamp:
            unchanged = known.status != "not_run" or (
                self.isolation is not None and not self.isolation.runs_custom_code
            )
            if unchanged:  # else it did not run because the worker was not there yet: retry
                return
        d = CustomDefinition(stem, path, stamp)
        self._defs[stem] = d
        if not ID.match(stem):
            d.status, d.reason = "error", "the file name must be lowercase letters, digits and _"
            return
        if st.st_size > MAX_FILE_BYTES:
            d.status, d.reason = "error", f"the file is larger than {MAX_FILE_BYTES // 1024} KB"
            return
        try:
            raw = await asyncio.to_thread(path.read_bytes)
            d.source = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            d.status, d.reason = "error", f"cannot read it: {exc}"
            return
        d.digest = hashlib.sha256(d.source.encode()).hexdigest()
        await self._describe(d)

    async def _describe(self, d: CustomDefinition) -> None:
        await self._wait_worker()
        if self.isolation is None:
            d.status, d.reason = "not_run", "the indicator worker is still starting"
            return
        if self.runner is None:
            d.status, d.reason = "not_run", self.isolation.notice or self.isolation.reason
            return
        try:
            schema = await self.runner.describe(d.digest, d.file, d.source)
            d.indicator = indicator_from_json(d.id, schema, d.file)
            d.status, d.reason, d.line, d.error = "ok", None, None, None
        except ScriptFailed as exc:
            self._failed(d, exc)
        except WorkerUnavailable as exc:
            d.status, d.reason = "not_run", str(exc)
        except (KeyError, TypeError, ValueError) as exc:
            d.status, d.reason = "error", f"the worker described it oddly: {exc}"

    def _failed(self, d: CustomDefinition, exc: ScriptFailed) -> None:
        d.line = exc.info.get("line")
        d.error = {
            "type": exc.info.get("type") or exc.kind,
            "message": exc.info.get("message") or exc.summary,
            "line": exc.info.get("line"),
            "traceback": exc.info.get("traceback") or "",
        }
        if exc.counts_for_quarantine:
            d.failures += 1
            if d.failures >= QUARANTINE_AFTER:
                d.status = "quarantined"
                d.reason = (
                    f"stopped after {d.failures} failures in a row (last: {exc.summary}); "
                    "fix the file to run it again"
                )
                log.warning("custom indicator %s quarantined: %s", d.file, exc.summary)
                return
        if d.indicator is None:  # it did not even load
            d.status = "error"
        d.reason = exc.summary

    # Values -------------------------------------------------------------------------

    def _check_runnable(self, d: CustomDefinition) -> tuple[IndicatorRunner, Indicator]:
        if d.status == "quarantined":
            raise ScriptFailed("quarantined", {"type": "Quarantined", "message": d.reason})
        if d.indicator is None or self.runner is None:
            raise WorkerUnavailable(d.reason or "this indicator cannot run here")
        return self.runner, d.indicator

    async def warmup(self, d: CustomDefinition, params: dict[str, Any]) -> int:
        runner, _ = self._check_runnable(d)
        try:
            return await runner.warmup(d.digest, d.file, d.source, params)
        except ScriptFailed as exc:
            self._failed(d, exc)
            raise

    async def compute(
        self,
        d: CustomDefinition,
        frame: pd.DataFrame,
        params: dict[str, Any],
        info: Info | None = None,
        *,
        warmup: int = 0,
    ) -> dict[str, pd.Series]:
        """Values from the worker. A script's own error is not counted toward the
        quarantine, so it is kept instead (owner, CI-1): the same script, parameters and
        bars answer the same error without running again, and a script that raised
        after more than half its deadline is not run again with those parameters for
        SLOW_ERROR_PAUSE seconds. Editing the file (a new hash) runs it at once.

        The first answer for new settings queues the save-time checks on these bars
        (`warmup`: the declared warmup the bars were read with)."""
        runner, indicator = self._check_runnable(d)
        bars = {c: frame[c].to_numpy(dtype="float64") for c in ("ts", "o", "h", "l", "c", "v")}
        info_json = info.to_json() if info is not None else None
        about = (d.digest, json.dumps([params, info_json], sort_keys=True))
        exact = (*about, _bars_digest(bars))
        if exact in self._errors:
            self._errors.move_to_end(exact)
            raise self._errors[exact]
        slow = self._slow.get(about)
        if slow is not None and time.monotonic() - slow[0] < SLOW_ERROR_PAUSE:
            raise slow[1]
        t = time.monotonic()
        try:
            out = await runner.compute(d.digest, d.file, d.source, params, bars, info_json)
            try:  # the worker is not trusted: what it sent back is checked again here
                series = indicator.check_wire(out, len(frame))
            except ResultError as exc:
                raise ScriptFailed("error", {"type": "ResultError", "message": str(exc)}) from None
        except ScriptFailed as exc:
            self._failed(d, exc)
            if exc.kind == "error":
                self._errors[exact] = exc
                while len(self._errors) > ERROR_CACHE_SIZE:
                    self._errors.popitem(last=False)
                if time.monotonic() - t > runner.limits.timeout_s / 2:
                    self._slow[about] = (time.monotonic(), exc)
            raise
        self._slow.pop(about, None)
        d.failures = 0
        if d.status != "ok":
            d.status, d.reason, d.line = "ok", None, None
        self._want_check(d, params, info, bars, series, warmup, runner.last_job_s)
        return series

    # Save-time checks ---------------------------------------------------------------

    def checks_for(self, d: CustomDefinition, params: dict[str, Any]) -> dict[str, Any] | None:
        """The check of this version with these settings, or why there is none."""
        key = _settings_key(params)
        if key in d.checks:
            return d.checks[key]
        if len(d.checks) >= CHECK_SETTINGS:
            why = (
                f"not checked for these settings: this version was checked with "
                f"{CHECK_SETTINGS} other settings already (their results are kept)"
            )
        elif len(self._pending) >= CHECK_QUEUE:
            why = "not checked yet: too many checks are waiting; a later request queues it"
        else:
            return None
        return {
            "version": (d.digest or "")[:12],
            "params": params,
            "lookahead": "not_checked",
            "warmup": "not_checked",
            "findings": [],
            "message": why,
        }

    def _want_check(self, d, params, info, bars, series, warmup, run_s) -> None:
        key = _settings_key(params)
        n = len(bars["ts"])
        if len(self._pending) >= CHECK_QUEUE:
            return
        if key in d.checks:
            old = d.checks[key]
            settled = "pending" not in (old["lookahead"], old["warmup"])
            if not (key in d.short and settled and n >= 2 * old["bars"]):
                return
            d.short.discard(key)  # checked again on these bars, in place of the short one
        elif len(d.checks) >= CHECK_SETTINGS:
            return
        result = {
            "version": d.digest[:12],
            "params": params,
            "instrument": info.instrument if info else None,
            "timeframe": info.timeframe if info else None,
            "price": info.price if info else None,
            "bars": n,
            "declared_warmup": int(warmup),
            "lookahead": "pending",
            "warmup": "pending",
            "findings": [],
        }
        d.checks[key] = result
        full = {k: s.to_numpy(dtype="float64") for k, s in series.items()}
        info_json = info.to_json() if info is not None else None
        self._pending.append(
            _CheckJob(d, params, info_json, bars, full, int(warmup), run_s, result)
        )
        if self._checker is None or self._checker.done():
            self._checker = asyncio.create_task(self._check_loop(), name="indicator-checks")

    async def close(self) -> None:
        self._pending.clear()
        if self._checker is not None:
            self._checker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._checker
            self._checker = None

    async def _check_loop(self) -> None:
        """One check at a time, CHECK_GAP apart, none within CHECK_STAGGER of a worker
        (re)start; checks of a file that changed since are dropped."""
        while self._pending:
            runner = self.runner
            if runner is None or runner.state == "stopped":
                self._pending.clear()
                return
            now = runner.clock()
            wait = (
                max(runner.ready_at + self.check_stagger, self._last_check + self.check_gap) - now
            )
            if not runner.up:  # (re)starting or dead: its pause starts once a new one is up
                wait = max(wait, 0.2)
            if wait > 0:
                await self.sleep(wait)
                continue
            job = self._pending.popleft()
            if self._defs.get(job.d.id) is not job.d:
                continue  # a newer version of the file
            started = runner.clock()
            try:
                await self._check(runner, job)
            except Exception:  # never let one check stop the others
                log.exception("check of %s", job.d.file)
                for name in ("lookahead", "warmup"):
                    if job.result[name] == "pending":
                        job.result[name] = "failed"
            self._last_check = runner.clock()
            self.check_log.append((job.d.id, started, self._last_check))

    async def _check(self, runner: IndicatorRunner, job: _CheckJob) -> None:
        d, result = job.d, job.result
        findings: list[dict[str, Any]] = result["findings"]
        n = len(job.bars["ts"])
        plan = _checks.plan(n, job.warmup)
        t = job.bars["ts"]
        deadline = runner.limits.timeout_s
        last = job.run_s  # how long the last run of this script took
        stop: str | None = None  # why the warmup run should not start either

        def budget(check: str) -> bool:
            if _checks.fits(last, deadline):
                return True
            result[check] = "skipped"
            findings.append(
                _checks.note(
                    check,
                    f"{check} check skipped: one run took {last:.1f} s, and a check run "
                    f"must fit in {_checks.RUN_SHARE * deadline:.1f} s "
                    f"({_checks.RUN_SHARE:.0%} of the {deadline:g} s deadline)",
                )
            )
            return False

        if plan.cut is None or plan.start is None:  # too few bars for one of the runs
            d.short.add(_settings_key(job.params))
        if plan.cut is None:
            result["lookahead"] = "skipped"
            findings.append(_checks.note("lookahead", plan.lookahead_skip or ""))
        elif budget("lookahead"):
            part, last, stop = await self._run_check(runner, job, "lookahead", 0, n - plan.cut)
            if part is not None:
                found = _checks.lookahead_findings(d.indicator, job.full, part, t)
                findings.extend(found)
                result["lookahead"] = _checks.state(found)

        if stop is not None:
            result["warmup"] = "skipped"
            findings.append(_checks.note("warmup", f"warmup check skipped: {stop}"))
        elif plan.start is None:
            result["warmup"] = "skipped"
            findings.append(_checks.note("warmup", plan.warmup_skip or ""))
        elif budget("warmup"):
            offset, last, _ = await self._run_check(runner, job, "warmup", plan.start, n)
            if offset is not None:
                found = _checks.warmup_findings(d.indicator, job.full, offset, plan, t)
                findings.extend(found)
                result["warmup"] = _checks.state(found)
        for f in findings:
            if f["level"] == "warning":
                log.info("custom indicator %s: %s", d.file, f["message"])

    async def _run_check(
        self, runner: IndicatorRunner, job: _CheckJob, check: str, start: int, stop: int
    ) -> tuple[dict[str, np.ndarray] | None, float, str | None]:
        """One shortened run in the worker, at the lowest priority, with the request's own
        parameters and `info`. Never counted toward the quarantine, never kept as the
        script's error: (values or None, seconds, why the next run should not start)."""
        d, result = job.d, job.result
        bars = {c: a[start:stop] for c, a in job.bars.items()}
        try:
            out = await runner.compute(
                d.digest, d.file, d.source, job.params, bars, job.info_json, priority="low"
            )
            values = d.indicator.check_wire(out, stop - start)
        except ScriptFailed as exc:
            took = runner.last_job_s
            if exc.kind == "timeout":
                result[check] = "timeout"
                why = f"the {check} run timed out ({exc.summary}); not counted against the file"
            else:
                result[check] = "failed"
                why = f"the {check} run on fewer bars failed: {exc.summary}"
            job.result["findings"].append(_checks.note(check, why))
            return None, took, why if exc.kind != "error" else None
        except ResultError as exc:
            result[check] = "failed"
            job.result["findings"].append(
                _checks.note(check, f"the {check} run on fewer bars returned bad values: {exc}")
            )
            return None, runner.last_job_s, None
        except WorkerUnavailable as exc:
            result[check] = "skipped"
            why = f"the indicator worker is not available: {exc}"
            job.result["findings"].append(_checks.note(check, why))
            return None, 0.0, why
        return {k: s.to_numpy() for k, s in values.items()}, runner.last_job_s, None


@dataclass
class _CheckJob:
    d: CustomDefinition
    params: dict[str, Any]
    info_json: dict[str, Any] | None
    bars: dict[str, np.ndarray]
    full: dict[str, np.ndarray]  # the chart request's own answer, checked
    warmup: int
    run_s: float
    result: dict[str, Any]


def _settings_key(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
