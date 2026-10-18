"""Bars of the fixed timeframes kept in the server's memory, for the read path only
(`SeriesReader`: `/api/candles` and the indicator routes). Design and decisions:
`docs/decisions-bar-cache.md`.

- **A run** per (source, instrument, timeframe, price): the bars `[lo, hi)` exactly as
  `SeriesReader` computes them from the store, `lo` and `hi` on bar boundaries. A request
  inside the run is a slice; one that reaches past it reads only what is missing (before
  `lo`, after `hi`) and the run grows.
- **Writes:** the store calls `_written` on every bump of a series' epoch, with the oldest
  time written. Every run of the series is cut from the bar holding `oldest - 1 h` (writing
  M1 can move the hour where M1 takes over from H1), or dropped (`oldest` None); what is left
  is stamped with the new epoch. A run is used only while its stamp equals the store's
  epoch, so a bump that did not say what it wrote drops it.
- **A write during a read:** the read keeps what it read only if the epoch did not move while
  it read; otherwise it reads the whole request again and returns it without keeping it.
- **Size:** at most `max_bars` bars per run (the oldest go first) and `max_bytes` in all,
  least recently used run first.

Stored bars only: the forming minute is folded in afterwards by the reader.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from openchart.market.aggregate import COLUMNS, empty_frame
from openchart.market.sessions import bar_start
from openchart.market.timeframes import Kind, Timeframe
from openchart.store.candles import CandleStore

MAX_BYTES = 16 * 2**20
MAX_BARS = 50_000
ROW_BYTES = 8 * len(COLUMNS)
# Writing M1 before the first stored minute moves the hour where M1 takes over from H1
# (`CandleStore.h1_boundary`) to the hour after the oldest minute written.
MARGIN = 3600

Key = tuple[str, str, str, str]  # source, instrument, timeframe, price
Load = Callable[[int, int], Awaitable[pd.DataFrame]]


@dataclass(frozen=True)
class _Run:
    lo: int
    hi: int
    epoch: int
    cols: dict[str, np.ndarray]

    def __len__(self) -> int:
        return len(self.cols["ts"])

    def window(self, lo: int, hi: int) -> dict[str, np.ndarray]:
        ts = self.cols["ts"]
        a, b = np.searchsorted(ts, lo, "left"), np.searchsorted(ts, hi, "left")
        return {c: v[a:b] for c, v in self.cols.items()}


def _columns(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    return {c: frame[c].to_numpy(copy=True) for c in COLUMNS}


def _frame(cols: dict[str, np.ndarray]) -> pd.DataFrame:
    if not len(cols["ts"]):
        return empty_frame()
    return pd.DataFrame({c: cols[c].copy() for c in COLUMNS})


class BarCache:
    def __init__(self, store: CandleStore, max_bytes: int = MAX_BYTES, max_bars: int = MAX_BARS):
        self.store = store
        self.max_bytes = max_bytes
        self.max_bars = max_bars
        self.nbytes = 0
        self.hits = self.misses = 0
        self._runs: OrderedDict[Key, _Run] = OrderedDict()
        self._tfs: dict[Key, Timeframe] = {}
        store.on_write(self._written)

    def __len__(self) -> int:
        return len(self._runs)

    def run_lengths(self) -> list[int]:
        return [len(r) for r in self._runs.values()]

    @staticmethod
    def covers(tf: Timeframe) -> bool:
        """Fixed timeframes built from stored M1/H1 (1m to 1h). Not the tick ones, and not
        4h/D/W, which have their own cache (`market.cache`)."""
        return tf.kind is Kind.FIXED

    async def read(
        self,
        source: str,
        instrument: str,
        tf: Timeframe,
        price: str,
        lo: int,
        hi: int,
        load: Load,
    ) -> pd.DataFrame:
        """The bars with `lo <= ts < hi` (bar boundaries), as `load(lo, hi)` would give them."""
        if lo >= hi:
            return empty_frame()
        key = (source, instrument, tf.name, price)
        epoch = self.store.epoch(source, instrument)
        run = self._runs.get(key)
        if run is not None and run.epoch != epoch:  # bumped without telling us
            self._drop(key)
            run = None
        if run is not None and run.lo <= lo and hi <= run.hi:
            self.hits += 1
            self._runs.move_to_end(key)
            return _frame(run.window(lo, hi))
        self.misses += 1
        joined = run is not None and run.lo <= hi and lo <= run.hi  # overlaps or touches
        if joined:
            assert run is not None
            parts = [(a, b) for a, b in ((lo, run.lo), (run.hi, hi)) if a < b]
        else:
            parts = [(lo, hi)]
        loaded = [(a, b, await load(a, b)) for a, b in parts]
        if self.store.epoch(source, instrument) != epoch:
            # Written while we read: not kept, and read again whole so the answer is one
            # consistent read, as without the cache.
            return await load(lo, hi)
        # From here to the end there is no await: nothing can write in between.
        pieces = [(a, b, _columns(f)) for a, b, f in loaded]
        if joined:
            assert run is not None
            pieces.append((run.lo, run.hi, run.cols))
        pieces.sort(key=lambda p: p[0])
        cols = {c: np.concatenate([p[2][c] for p in pieces]) for c in COLUMNS}
        new = _Run(pieces[0][0], pieces[-1][1], epoch, cols)
        answer = new.window(lo, hi)
        if self._runs.get(key) is run:  # else another read kept a run meanwhile: keep that
            self._keep(key, tf, new)
        return _frame(answer)

    def _keep(self, key: Key, tf: Timeframe, run: _Run) -> None:
        if len(run) > self.max_bars:  # the oldest go: the run starts at its first kept bar
            cut = len(run) - self.max_bars
            cols = {c: v[cut:].copy() for c, v in run.cols.items()}
            run = _Run(int(cols["ts"][0]), run.hi, run.epoch, cols)
        self._drop(key)
        size = len(run) * ROW_BYTES
        if size > self.max_bytes:
            return
        self._runs[key] = run
        self._tfs[key] = tf
        self.nbytes += size
        while self.nbytes > self.max_bytes:
            self._drop(next(iter(self._runs)))

    def _drop(self, key: Key) -> None:
        run = self._runs.pop(key, None)
        if run is not None:
            self.nbytes -= len(run) * ROW_BYTES
            self._tfs.pop(key, None)

    def _written(self, source: str, instrument: str, oldest: int | None, epoch: int) -> None:
        for key in [k for k in self._runs if k[0] == source and k[1] == instrument]:
            run = self._runs[key]
            if oldest is None:
                self._drop(key)
                continue
            cut = bar_start(oldest - MARGIN, self._tfs[key])
            if cut <= run.lo:
                self._drop(key)
                continue
            if cut < run.hi:
                kept = run.window(run.lo, cut)
                self.nbytes -= (len(run) - len(kept["ts"])) * ROW_BYTES
                run = _Run(run.lo, cut, epoch, {c: v.copy() for c, v in kept.items()})
            else:
                run = _Run(run.lo, run.hi, epoch, run.cols)
            self._runs[key] = run
