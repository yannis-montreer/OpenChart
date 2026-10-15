"""Your own indicator files (`<data dir>/indicators/*.py`), as the server knows them.

The server never imports them, and does not even read their schema itself: it sends each
file's source to the worker, which answers what the file declares (`describe`) and
computes its values. Files are read again when their modification time or size changes,
so no restart is needed after editing one.

A definition that crashes the worker, times out or hits the memory limit 3 times in a
row is stopped ("quarantined") until its file changes, so one bad script cannot keep the
worker restarting for everyone. Its own exceptions are shown, not counted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from openchart.indicators._base import Indicator, Output, Param
from openchart.indicators.worker.isolation import Isolation
from openchart.indicators.worker.runner import IndicatorRunner, ScriptFailed, WorkerUnavailable

log = logging.getLogger(__name__)

ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_FILE_BYTES = 256 * 1024
QUARANTINE_AFTER = 3
WORKER_WAIT = 90.0  # seconds a request waits for the worker's first start
ERROR_CACHE_SIZE = 256  # a script's own errors kept, by script, parameters and bars
SLOW_ERROR_PAUSE = 30.0  # seconds a slow error is answered from memory


def _bars_digest(bars: dict[str, np.ndarray]) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    for name in sorted(bars):
        h.update(name.encode())
        h.update(np.ascontiguousarray(bars[name], dtype="<f8").tobytes())
    return h.hexdigest()


def _not_here(*_args: Any) -> Any:
    raise RuntimeError("custom indicators are computed in the worker")


def indicator_from_json(indicator_id: str, d: dict[str, Any]) -> Indicator:
    """The schema the worker described, for validating parameters here."""
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
            )
        )
    outputs = [Output(o["name"], o["label"], o["color"], o["width"]) for o in d["outputs"]]
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
        self._scan = asyncio.Lock()
        # A script's own errors, by (hash, params, bars), and slow ones by (hash, params).
        self._errors: OrderedDict[tuple[str, str, str], ScriptFailed] = OrderedDict()
        self._slow: dict[tuple[str, str], tuple[float, ScriptFailed]] = {}

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
            d.indicator = indicator_from_json(d.id, schema)
            d.status, d.reason, d.line = "ok", None, None
        except ScriptFailed as exc:
            self._failed(d, exc)
        except WorkerUnavailable as exc:
            d.status, d.reason = "not_run", str(exc)
        except (KeyError, TypeError, ValueError) as exc:
            d.status, d.reason = "error", f"the worker described it oddly: {exc}"

    def _failed(self, d: CustomDefinition, exc: ScriptFailed) -> None:
        d.line = exc.info.get("line")
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
        self, d: CustomDefinition, frame: pd.DataFrame, params: dict[str, Any]
    ) -> dict[str, pd.Series]:
        """Values from the worker. A script's own error is not counted toward the
        quarantine, so it is kept instead (owner, CI-1): the same script, parameters and
        bars answer the same error without running again, and a script that raised
        after more than half its deadline is not run again with those parameters for
        SLOW_ERROR_PAUSE seconds. Editing the file (a new hash) runs it at once."""
        runner, indicator = self._check_runnable(d)
        bars = {c: frame[c].to_numpy(dtype="float64") for c in ("ts", "o", "h", "l", "c", "v")}
        about = (d.digest, json.dumps(params, sort_keys=True))
        exact = (*about, _bars_digest(bars))
        if exact in self._errors:
            self._errors.move_to_end(exact)
            raise self._errors[exact]
        slow = self._slow.get(about)
        if slow is not None and time.monotonic() - slow[0] < SLOW_ERROR_PAUSE:
            raise slow[1]
        t = time.monotonic()
        try:
            out = await runner.compute(d.digest, d.file, d.source, params, bars)
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
        series = {}
        for o in indicator.outputs:  # the worker checked the shape; check it again here
            values = np.asarray(out.get(o.name, ()), dtype="float64")
            if len(values) != len(frame):
                raise ScriptFailed(
                    "error",
                    {
                        "type": "ValueError",
                        "message": f"output {o.name!r} has {len(values)} values "
                        f"for {len(frame)} bars",
                    },
                )
            series[o.name] = pd.Series(values)
        return series
