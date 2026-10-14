"""History backfill and gap-fill for M1 candles, and deep H1 history.

Downloads run newest to oldest (`download_backward`): H1 first, as far back as the
provider has it (a few dozen requests, then 1h and higher charts are complete), then
M1 back to the configured start. What is stored is always one block ending at the
present, and an interrupted download continues backward from its oldest candle. Where
each download finished is recorded per instrument (`CandleStore.download_marks`).

Invariant: stored candles of one granularity for a `(source, instrument)` cover one contiguous range
`[first_ts, last_ts]` (apart from market closures and minutes without ticks). The tail
is extended forward and the head backward, each batch committed in order, so a run
interrupted at any point can simply be repeated: it resumes where it stopped.
Writes are upserts, so repeating a run never duplicates rows.

Holes inside the stored range can still appear: if the process is paused (the computer
sleeps), the once-a-minute refresh of the last few minutes writes candles ahead of the
hole on waking, and the tail gap-fill then starts after it. `repair` finds and refills
such holes; the feed runs it over the last few days on every start, reconnect and wake-up.

A hole of at least `CLOSURE_MINUTES` open minutes that the broker has no candles for,
even when asked again, is a closure the trading hours do not know about (a holiday, an
early close). `repair` records those, and the trading clock stops during them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from openchart.market.gaps import Gap, find_gaps
from openchart.market.sessions import TradingHours
from openchart.providers.base import Candle, Provider, ProviderError
from openchart.store.candles import STEP_SECONDS, CandleStore

log = logging.getLogger(__name__)

M1_SECONDS = 60
# Deep history is requested from here; the provider answers from its first candle
# (OANDA XAU_USD: 2006-03-19).
DEEP_HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)
# One head window per transaction: about one request's worth of candles.
HEAD_WINDOW = {"M1": timedelta(days=3), "H1": timedelta(days=180)}

# Interior holes in this much recent M1 are repaired on every start, reconnect and wake-up.
RECENT_REPAIR = timedelta(days=3)
# Holes within this span are refetched with one range (OANDA: 5000 M1 candles a page).
REPAIR_WINDOW = 3 * 86400
# Shorter holes are a few missing broker minutes, not a closure: they do not bend lines.
CLOSURE_MINUTES = 30

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


@dataclass
class RepairResult:
    instrument: str
    holes: list[Gap] = field(default_factory=list)  # found before the repair
    filled: int = 0  # candles written into them
    requests: int = 0
    closures_changed: bool = False


def _fetch_windows(holes: list[Gap], span: int) -> list[tuple[int, int]]:
    """Group holes into `[start, end)` fetch ranges of about `span` seconds each."""
    windows: list[tuple[int, int]] = []
    for gap in holes:
        start, end = gap.before + M1_SECONDS, gap.after
        if windows and end - windows[-1][0] <= span:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return windows


async def repair(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    hours: TradingHours | None,
    *,
    start: int | None = None,
    end: int | None = None,
    min_open_minutes: int = 2,
    source: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RepairResult:
    """Refill holes inside stored M1 between `start` and `end` (default: everything).

    A hole is a run of missing minutes, at least `min_open_minutes` of them while the
    market should be open (`find_gaps`). Only missing candles are written, so stored
    ones are never replaced, and a hole the broker has no data for (a holiday, an early
    close) costs a request and changes nothing. The tail after the newest candle is
    `gap_fill`'s job.
    """
    source = source or provider.name
    result = RepairResult(instrument)
    stamps = await _stamps(store, source, instrument, start, end)
    result.holes = find_gaps(stamps, hours, min_open_minutes)
    windows = _fetch_windows(result.holes, REPAIR_WINDOW)
    for i, (lo, hi) in enumerate(windows):
        have = set(await store.timestamps(source, instrument, lo, hi))
        candles = provider.fetch_candles(instrument, _dt(lo), _dt(hi))
        missing = [c async for c in candles if c.ts not in have]
        result.requests += 1
        if missing:
            result.filled += await store.upsert(source, instrument, missing)
        if progress:
            progress(i + 1, len(windows))
    if hours is not None and min_open_minutes <= CLOSURE_MINUTES:
        # Every hole of CLOSURE_MINUTES or more was just asked for again: what is left
        # is where the broker has nothing.
        result.closures_changed = await learn_closures(store, source, instrument, hours, start, end)
    return result


async def _stamps(
    store: CandleStore, source: str, instrument: str, start: int | None, end: int | None
) -> list[int]:
    stamps = await store.timestamps(source, instrument, start, end)
    if start is not None and stamps:
        # A hole that began before `start` ends inside the range: include its left edge.
        before = await store.last_ts(source, instrument, before=stamps[0])
        if before is not None:
            stamps.insert(0, before)
    return stamps


async def learn_closures(
    store: CandleStore,
    source: str,
    instrument: str,
    hours: TradingHours,
    start: int | None = None,
    end: int | None = None,
) -> bool:
    """Store the holes of `CLOSURE_MINUTES`+ open minutes in `[start, end)` as closures.

    Only call this right after those holes were requested from the broker again.
    Returns True if the stored closures changed.
    """
    stamps = await _stamps(store, source, instrument, start, end)
    if len(stamps) < 2:
        return False
    gaps = find_gaps(stamps, hours, CLOSURE_MINUTES)
    found = [(g.before + M1_SECONDS, g.after) for g in gaps]
    return await store.replace_closures(source, instrument, stamps[0], stamps[-1], found)


async def repair_recent(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    hours: TradingHours | None,
    *,
    source: str | None = None,
    lookback: timedelta = RECENT_REPAIR,
) -> RepairResult:
    """`repair` over the last few days: one request if anything is missing, else none."""
    start = int((datetime.now(UTC) - lookback).timestamp())
    result = await repair(provider, store, instrument, hours, start=start, source=source)
    if result.filled:
        holes = ", ".join(
            f"{_dt(g.before + M1_SECONDS):%m-%d %H:%M}-{_dt(g.after):%H:%M}" for g in result.holes
        )
        log.info("repaired %s: %d M1 candles filled in %s", instrument, result.filled, holes)
    return result


@dataclass
class DownloadResult:
    instrument: str
    granularity: str
    written: int
    oldest: int | None  # oldest stored candle after the download
    complete: bool  # reached `start`, or the provider has nothing older


async def download_backward(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    start: int | None,
    *,
    source: str | None = None,
    granularity: str = "M1",
    progress: Progress | None = None,
    max_pages: int | None = None,
) -> DownloadResult:
    """Extend stored history backward, page by page, from the oldest stored candle (or
    from the present when nothing is stored) to `start` (None: as far as the provider
    goes). Each page is committed on its own, so stopping at any point loses nothing.

    Stored history must reach the present first (`gap_fill`), so that the result is
    one contiguous block. `max_pages` stops early (not complete) after that many pages.
    """
    source = source or provider.name
    g = granularity
    cursor = await store.first_ts(source, instrument, g)
    written = pages = 0
    while start is None or cursor is None or cursor > start:
        if max_pages is not None and pages >= max_pages:
            return DownloadResult(instrument, g, written, cursor, False)
        pages += 1
        end = None if cursor is None else _dt(cursor)
        page = await provider.fetch_page_before(instrument, end, g)
        page = [c for c in page if cursor is None or c.ts < cursor]
        if not page:  # the provider has nothing older (nothing at all: done as of now)
            mark = cursor if cursor is not None else int(time.time())
            await store.mark_download_complete(source, instrument, g, mark)
            return DownloadResult(instrument, g, written, cursor, True)
        oldest = page[0].ts
        keep = [c for c in page if start is None or c.ts >= start]
        written += await store.upsert(source, instrument, keep, g)
        if cursor is not None and oldest >= cursor:
            raise ProviderError(f"{g} download of {instrument} stalled at {cursor}")
        cursor = oldest
        if progress:
            progress(written, max(oldest, start or oldest), "backward")
    await store.mark_download_complete(source, instrument, g, start)
    return DownloadResult(instrument, g, written, await store.first_ts(source, instrument, g), True)


async def deep_fill(
    provider: Provider,
    store: CandleStore,
    instrument: str,
    *,
    source: str | None = None,
    progress: Progress | None = None,
) -> DownloadResult:
    """H1 history as far back as the provider has it, kept up to date.

    The first run downloads everything newest to oldest (OANDA XAU_USD: about 20 years,
    ~30 requests); later runs fetch what is new, and finish an interrupted download.
    """
    source = source or provider.name
    new = 0
    if await store.last_ts(source, instrument, "H1") is not None:
        tail = await gap_fill(provider, store, instrument, source=source, granularity="H1")
        new = tail.written
    h1_from, _ = await store.download_marks(source, instrument)
    if h1_from is not None:
        first = await store.first_ts(source, instrument, "H1")
        return DownloadResult(instrument, "H1", new, first, True)
    log.info("downloading deep H1 history for %s, newest first", instrument)
    result = await download_backward(
        provider, store, instrument, None, source=source, granularity="H1", progress=progress
    )
    result.written += new
    return result


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
