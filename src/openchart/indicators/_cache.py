"""Computed indicator values, kept in the server (CI-5, design 3.8).

- **The key** is everything the values depend on: the indicator's kind, id and version
  (a custom file's hash), its inputs **without the style ones** (colour and width change
  how a line is drawn, never its values, so a restyle never recomputes), the request's
  `info`, and a digest of the very bars computed on, warmup included. A code change and
  rewritten bars (a repair or refill) are therefore new keys: nothing has to be told to
  clear anything, and an old entry is simply never asked for again.
- **The size** is bounded (about 100 MB of values), least recently used first.
- **One computation for every tab:** identical requests that arrive while one is being
  computed wait for it instead of computing again.
- **The forming bar** changes with every tick, so its window is a new key each time; a
  window that ends on the forming bar is computed at most once a second (`forming_every`)
  and in between answered with the last result for the same window (same first and last
  bar). A new bar is a new window and is never held back.

Only successes are kept; a failure is the caller's (the custom indicators keep their own
errors, `_custom.py`).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

import pandas as pd

MAX_BYTES = 100 * 2**20
FORMING_EVERY = 1.0  # seconds between two computations of one forming window

Series = dict[str, pd.Series]


class ValueCache:
    def __init__(
        self,
        max_bytes: int = MAX_BYTES,
        forming_every: float = FORMING_EVERY,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_bytes = max_bytes
        self.forming_every = forming_every
        self.clock = clock
        self.nbytes = 0
        self.hits = self.misses = 0
        self._entries: OrderedDict[Hashable, tuple[Series, int]] = OrderedDict()
        self._flights: dict[Hashable, asyncio.Future[Series]] = {}
        self._forming: dict[Hashable, tuple[float, Hashable]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _hit(self, key: Hashable) -> Series | None:
        got = self._entries.get(key)
        if got is None:
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return got[0]

    def _store(self, key: Hashable, series: Series) -> None:
        size = sum(int(s.to_numpy().nbytes) for s in series.values())
        if size > self.max_bytes:
            return  # larger than the whole cache: not kept
        old = self._entries.pop(key, None)
        if old is not None:
            self.nbytes -= old[1]
        self._entries[key] = (series, size)
        self.nbytes += size
        while self.nbytes > self.max_bytes:
            _, (_, dropped) = self._entries.popitem(last=False)
            self.nbytes -= dropped

    async def get(
        self,
        key: Hashable,
        compute: Callable[[], Awaitable[Series]],
        *,
        window: Hashable | None = None,
    ) -> Series:
        """The values for `key`, computed by `compute` only when needed. `window` is given
        for a window that ends on the forming bar (it leaves out the bars' digest)."""
        if window is not None:
            last = self._forming.get(window)
            if last is not None and self.clock() - last[0] < self.forming_every:
                got = self._hit(last[1])
                if got is not None:
                    return got
        got = self._hit(key)
        if got is not None:
            return got
        flight = self._flights.get(key)
        if flight is not None:
            return await asyncio.shield(flight)
        self.misses += 1
        flight = asyncio.get_running_loop().create_future()
        self._flights[key] = flight
        try:
            series = await compute()
        except BaseException as exc:
            flight.set_exception(exc)
            flight.exception()  # retrieved: the waiters, if any, get it too
            raise
        finally:
            self._flights.pop(key, None)
        flight.set_result(series)
        self._store(key, series)
        if window is not None:
            self._forming[window] = (self.clock(), key)
            if len(self._forming) > 1024:  # windows of charts no longer open
                self._forming.pop(next(iter(self._forming)))
        return series

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "bytes": self.nbytes,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
        }
