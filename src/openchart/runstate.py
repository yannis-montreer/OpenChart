"""Whether the last server run stopped cleanly: a small file in the data folder.

`run-state.json` holds `{"state": "running" | "clean", "ts": <epoch seconds>}`:

- at start the server reads it (`begin`) and writes `running` with the start time;
- when the server stops properly (`Server.stop`, which Ctrl+C, the Windows service's stop
  and `docker stop` all end in: uvicorn's graceful shutdown) it writes `clean` with the
  time the stop began (`clean_stop`).

So at the next start `running` means the last run never got to stop (a crash, a power cut,
a kill), and `clean` carries the moment the engine went down, which is the length of the
downtime. No file (a first start, or an install from before this file existed) is not a
crash: nothing is known, and nothing is claimed.

It lives beside the database, not in it: no schema change, and it is written even when the
database could not be opened.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RUN_STATE_NAME = "run-state.json"


@dataclass(frozen=True)
class PreviousRun:
    state: str | None  # "running" (did not stop cleanly), "clean", or None (unknown)
    ts: float | None  # when it stopped (clean) or started (running)

    @property
    def unclean(self) -> bool:
        return self.state == "running"


class RunState:
    def __init__(self, data_dir: Path, now: Callable[[], float] = time.time):
        self.path = Path(data_dir) / RUN_STATE_NAME
        self.now = now

    def read(self) -> PreviousRun:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            state, ts = data["state"], float(data["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return PreviousRun(None, None)
        return PreviousRun(state if state in ("running", "clean") else None, ts)

    def _write(self, state: str, ts: float) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"state": state, "ts": ts}), encoding="utf-8")
        os.replace(tmp, self.path)

    def begin(self) -> PreviousRun:
        """The previous run's state, then `running`: the marker is cleared at start."""
        previous = self.read()
        self._write("running", self.now())
        return previous

    def clean_stop(self, ts: float | None = None) -> None:
        self._write("clean", self.now() if ts is None else ts)
