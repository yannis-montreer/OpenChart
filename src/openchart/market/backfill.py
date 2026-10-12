"""History backfill and gap-fill for M1 candles.

Invariant: stored M1 candles for a `(source, instrument)` cover one contiguous range
`[first_ts, last_ts]` (apart from market closures and minutes without ticks). The tail
is extended forward and the head backward, each batch committed in order, so a run
interrupted at any point can simply be repeated: it resumes where it stopped.
Writes are upserts, so repeating a run never duplicates rows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openchart.providers.base import Candle, Provider
from openchart.store.candles import CandleStore

log = logging.getLogger(__name__)

M1_SECONDS = 60

# (rows written so far, ts, direction). Forward: newest candle stored so far.
# Backward: how far back history has been searched.
Progress = Callable[[int, int, str], None]


@dataclass
class BackfillResult:
    instrument: str
    written: int
    first_ts: int | None
    last_ts: int | None


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


async def backfill(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    start: datetime,
    end: datetime | None = None,
    *,
    source: str | None = None,
    head_window: timedelta = timedelta(days=3),
    batch_size: int = 5000,
    progress: Progress | None = None,
) -> BackfillResult:
    """Make stored M1 candles cover `[start, end)` (end defaults to now)."""
    source = source or provider.name
    end = end or datetime.now(UTC)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    written = 0

    def report(ts: int, direction: str) -> None:
        if progress:
            progress(written, ts, direction)

    first = await store.first_ts(source, instrument)
    last = await store.last_ts(source, instrument)

    # Tail: forward from the newest stored candle (or from `start` if nothing is stored).
    tail_from = start_ts if last is None else max(start_ts, last + M1_SECONDS)
    if tail_from < end_ts:
        batch: list[Candle] = []
        async for candle in provider.fetch_candles(instrument, _dt(tail_from), end):
            batch.append(candle)
            if len(batch) >= batch_size:
                written += await store.upsert_m1(source, instrument, batch)
                report(batch[-1].ts, "forward")
                batch = []
        if batch:
            written += await store.upsert_m1(source, instrument, batch)
            report(batch[-1].ts, "forward")

    # Head: backward from the oldest stored candle, one window per transaction.
    if first is not None and start_ts < first:
        window_end = first
        while window_end > start_ts:
            window_start = max(start_ts, window_end - int(head_window.total_seconds()))
            candles = [
                c
                async for c in provider.fetch_candles(
                    instrument, _dt(window_start), _dt(window_end)
                )
            ]
            written += await store.upsert_m1(source, instrument, candles)
            report(window_start, "backward")
            window_end = window_start

    return BackfillResult(
        instrument,
        written,
        await store.first_ts(source, instrument),
        await store.last_ts(source, instrument),
    )


async def gap_fill(
    provider: Provider, store: CandleStore, instrument: str, *, source: str | None = None
) -> BackfillResult:
    """Fetch everything after the newest stored candle. No-op if nothing is stored."""
    source = source or provider.name
    last = await store.last_ts(source, instrument)
    if last is None:
        return BackfillResult(instrument, 0, None, None)
    result = await backfill(provider, store, instrument, _dt(last), source=source)
    if result.written:
        log.info("gap-fill %s: %d candles up to %s", instrument, result.written, result.last_ts)
    return result
