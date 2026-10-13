"""History backfill and gap-fill for M1 candles, and deep H1 history.

Invariant: stored candles of one granularity for a `(source, instrument)` cover one contiguous range
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
from openchart.store.candles import STEP_SECONDS, CandleStore

log = logging.getLogger(__name__)

M1_SECONDS = 60
# Deep history is requested from here; the provider answers from its first candle
# (OANDA XAU_USD: 2006-03-19).
DEEP_HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)
# One head window per transaction: about one request's worth of candles.
HEAD_WINDOW = {"M1": timedelta(days=3), "H1": timedelta(days=180)}

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
    granularity: str = "M1",
    head_window: timedelta | None = None,
    batch_size: int = 5000,
    progress: Progress | None = None,
) -> BackfillResult:
    """Make stored candles of `granularity` cover `[start, end)` (end defaults to now)."""
    source = source or provider.name
    g = granularity
    head_window = head_window or HEAD_WINDOW[g]
    end = end or datetime.now(UTC)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    written = 0

    def report(ts: int, direction: str) -> None:
        if progress:
            progress(written, ts, direction)

    first = await store.first_ts(source, instrument, g)
    last = await store.last_ts(source, instrument, g)

    # Tail: forward from the newest stored candle (or from `start` if nothing is stored).
    tail_from = start_ts if last is None else max(start_ts, last + STEP_SECONDS[g])
    if tail_from < end_ts:
        batch: list[Candle] = []
        async for candle in provider.fetch_candles(instrument, _dt(tail_from), end, g):
            batch.append(candle)
            if len(batch) >= batch_size:
                written += await store.upsert(source, instrument, batch, g)
                report(batch[-1].ts, "forward")
                batch = []
        if batch:
            written += await store.upsert(source, instrument, batch, g)
            report(batch[-1].ts, "forward")

    # Head: backward from the oldest stored candle, one window per transaction.
    if first is not None and start_ts < first:
        window_end = first
        while window_end > start_ts:
            window_start = max(start_ts, window_end - int(head_window.total_seconds()))
            candles = [
                c
                async for c in provider.fetch_candles(
                    instrument, _dt(window_start), _dt(window_end), g
                )
            ]
            written += await store.upsert(source, instrument, candles, g)
            report(window_start, "backward")
            window_end = window_start

    return BackfillResult(
        instrument,
        written,
        await store.first_ts(source, instrument, g),
        await store.last_ts(source, instrument, g),
    )


async def gap_fill(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    *,
    source: str | None = None,
    granularity: str = "M1",
) -> BackfillResult:
    """Fetch everything after the newest stored candle. No-op if nothing is stored."""
    source = source or provider.name
    last = await store.last_ts(source, instrument, granularity)
    if last is None:
        return BackfillResult(instrument, 0, None, None)
    result = await backfill(
        provider, store, instrument, _dt(last), source=source, granularity=granularity
    )
    if result.written:
        log.info(
            "gap-fill %s %s: %d candles up to %s",
            instrument,
            granularity,
            result.written,
            result.last_ts,
        )
    return result


async def deep_fill(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    *,
    source: str | None = None,
    progress: Progress | None = None,
) -> BackfillResult:
    """H1 history as far back as the provider has it, then kept up to date.

    The first run downloads everything (OANDA XAU_USD: about 20 years, ~30 requests);
    later runs only fetch what is new.
    """
    source = source or provider.name
    if await store.last_ts(source, instrument, "H1") is not None:
        return await gap_fill(provider, store, instrument, source=source, granularity="H1")
    log.info("downloading deep H1 history for %s", instrument)
    return await backfill(
        provider,
        store,
        instrument,
        DEEP_HISTORY_START,
        source=source,
        granularity="H1",
        progress=progress,
    )


async def refresh_recent(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    lookback: timedelta = timedelta(minutes=15),
    *,
    source: str | None = None,
) -> int:
    """Re-fetch the last few minutes of M1 from the provider and upsert them.

    Live M1 bars built from streamed ticks can differ slightly from the broker's own
    candles (the stream is sampled). This replaces them with the official version.
    """
    source = source or provider.name
    end = datetime.now(UTC)
    candles = [c async for c in provider.fetch_candles(instrument, end - lookback, end)]
    return await store.upsert_m1(source, instrument, candles)
