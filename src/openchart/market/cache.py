"""Cache of complete 4h, D and W bars (table `candles_cache`).

Bars are derived from the base data (H1 where M1 does not reach, M1 from there; see
`CandleStore.read_base_frame`), exactly as an uncached request would compute them.
Only complete bars are stored. The cache grows forward from its newest bar: `extend`
computes the periods that closed since then, in chunks. Any M1 or H1 write deletes
the cached bars it affects (`CandleStore.upsert`), so `extend` simply refills them.

The store's epoch counter detects writes that happen while `extend` is computing; the
chunk is then dropped and recomputed from the fresh data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable

from openchart.market.aggregate import resample_full
from openchart.market.sessions import bar_bounds, bar_start
from openchart.market.timeframes import TIMEFRAMES, Timeframe
from openchart.store.candles import CACHED_TIMEFRAMES, CandleStore

log = logging.getLogger(__name__)

CHUNK_SECONDS = 400 * 86400  # base data read per step: at most ~400 days of M1
MAX_RETRIES = 5


def is_cached(tf: Timeframe) -> bool:
    return tf.name in CACHED_TIMEFRAMES


class CandleCache:
    def __init__(self, store: CandleStore, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def extend_all(self, source: str, instrument: str) -> int:
        written = 0
        for name in CACHED_TIMEFRAMES:
            written += await self.extend(source, instrument, TIMEFRAMES[name])
        return written

    async def extend(self, source: str, instrument: str, tf: Timeframe) -> int:
        """Cache every complete `tf` bar after the newest cached one. Returns bars written."""
        async with self._locks[(source, instrument, tf.name)]:
            written = await self._refill(source, instrument, tf)
            retries = 0
            start = await self._resume_point(source, instrument, tf)
            done = bar_start(self.clock(), tf)  # bars starting before this are complete
            while start is not None and start < done:
                step_end = max(bar_start(start + CHUNK_SECONDS, tf), bar_bounds(start, tf)[1])
                end = min(done, step_end)
                epoch = self.store.epoch(source, instrument)
                frame = await self.store.read_base_frame(source, instrument, start, end, None)
                bars = resample_full(frame, tf)
                if self.store.epoch(source, instrument) != epoch:
                    # Base data changed while we read it: recompute from the cache's end.
                    retries += 1
                    if retries > MAX_RETRIES:
                        log.warning(
                            "cache %s %s: data keeps changing, will retry later",
                            instrument,
                            tf.name,
                        )
                        break
                    start = await self._resume_point(source, instrument, tf)
                    continue
                # Queued before any later write's invalidation, so a write that starts
                # after this point still deletes these rows if they are affected.
                written += await self.store.write_cache(source, instrument, tf.name, bars)
                start = end
            return written

    async def _refill(self, source: str, instrument: str, tf: Timeframe) -> int:
        """Recompute the ranges dropped from inside the cache (`cache_dirty`), for example
        while a download extends the history backward. A range whose data changes while
        it is computed stays dirty and is done on the next call."""
        written = 0
        for start, end in await self.store.cache_dirty(source, instrument, tf.name):
            epoch = self.store.epoch(source, instrument)
            frame = await self.store.read_base_frame(source, instrument, start, end, None)
            bars = resample_full(frame, tf)
            if self.store.epoch(source, instrument) != epoch:
                continue
            written += await self.store.write_cache(
                source, instrument, tf.name, bars, refilled=(start, end)
            )
        return written

    async def _resume_point(self, source: str, instrument: str, tf: Timeframe) -> int | None:
        last = await self.store.cache_last_ts(source, instrument, tf.name)
        if last is not None:
            return bar_bounds(last, tf)[1]
        first = await self.store.first_base_ts(source, instrument, use_h1=True)
        return None if first is None else bar_start(first, tf)

    async def rebuild(self, source: str, instrument: str) -> int:
        await self.store.clear_cache(source, instrument)
        return await self.extend_all(source, instrument)
