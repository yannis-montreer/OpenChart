"""Live candles from ticks.

For each instrument the builder always keeps the forming M1 bar. When it closes it is
persisted (through `on_m1_closed`) and folded into every subscribed higher timeframe.
A higher-timeframe live bar is therefore "closed M1 bars of this period" plus "the
forming M1 bar", the same arithmetic as the history derived from stored M1, so live
and historical bars agree. Sub-minute bars (1s to 30s) are built straight from ticks.

The first M1 bar after a start or a reconnect began before we were listening, so it
is incomplete: it is shown live but not persisted (the feed's REST refresh stores the
broker's version). Call `mark_gap` after a reconnect.

Bars close on the first tick of the next period, or from the clock (`on_clock`) once
the period is over, so a bar still closes when the market goes quiet. Ticks older than
the forming bar are dropped.

Subscribing to a timeframe seeds its current bar from stored M1 (`load_m1`) plus a
short in-memory ring of recently closed M1 bars, which covers bars not yet written.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openchart.market.aggregate import Bar, merge_bars
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import M1, Kind, Timeframe
from openchart.providers.base import Candle, Tick

log = logging.getLogger(__name__)

RECENT_M1 = 30  # closed M1 bars kept in memory per instrument


@dataclass(frozen=True)
class CandleUpdate:
    instrument: str
    timeframe: str
    candle: Candle
    closed: bool


OnUpdate = Callable[[CandleUpdate], None]
OnM1Closed = Callable[[str, Candle], None]
LoadM1 = Callable[[str, int, int], Awaitable[list[Candle]]]  # instrument, start, end


class _Series:
    """Current bar of one subscribed (instrument, timeframe)."""

    def __init__(self, tf: Timeframe, ts: float):
        self.tf = tf
        self.refs = 0
        self.start, self.end = bar_bounds(ts, tf)
        self.base: Bar | None = None  # closed M1 bars of this period (tf >= 1m)
        self.bar: Bar | None = None  # sub-minute bar built from ticks

    def roll(self, ts: float) -> None:
        self.start, self.end = bar_bounds(ts, self.tf)
        self.base = self.bar = None


class CandleBuilder:
    def __init__(
        self,
        on_update: OnUpdate | None = None,
        on_m1_closed: OnM1Closed | None = None,
        load_m1: LoadM1 | None = None,
        close_grace: float = 3.0,
    ):
        self.listeners: list[OnUpdate] = [on_update] if on_update else []
        self.on_m1_closed = on_m1_closed
        self.load_m1 = load_m1
        self.close_grace = close_grace
        self._m1: dict[str, Bar] = {}  # forming M1 bar per instrument
        self._recent: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=RECENT_M1))
        self._series: dict[str, dict[str, _Series]] = defaultdict(dict)
        self._joined_midway: set[str] = set()  # instruments whose forming M1 is partial
        self.late_ticks = 0

    # Subscriptions

    async def subscribe(self, instrument: str, tf: Timeframe, now: float) -> None:
        existing = self._series[instrument].get(tf.name)
        if existing is not None:
            existing.refs += 1
            return
        series = _Series(tf, now)
        if tf.kind is not Kind.TICK:
            series.base = await self._seed(instrument, series.start, series.end)
        if tf.name in self._series[instrument]:  # subscribed concurrently while seeding
            self._series[instrument][tf.name].refs += 1
            return
        series.refs = 1
        self._series[instrument][tf.name] = series

    def unsubscribe(self, instrument: str, tf: Timeframe) -> None:
        series = self._series[instrument].get(tf.name)
        if series is not None:
            series.refs -= 1
            if series.refs <= 0:
                del self._series[instrument][tf.name]

    def subscriptions(self, instrument: str) -> list[str]:
        return list(self._series[instrument])

    async def _seed(self, instrument: str, start: int, end: int) -> Bar | None:
        """Closed M1 bars in [start, end) before the forming M1 bar."""
        stored: list[Candle] = []
        if self.load_m1 is not None:
            stored = await self.load_m1(instrument, start, end)
        forming = self._m1.get(instrument)
        limit = forming.ts if forming else end
        by_ts = {c.ts: Bar.from_candle(c) for c in stored}
        by_ts.update({b.ts: b for b in self._recent[instrument]})  # fresher than the DB
        bars = [by_ts[ts] for ts in sorted(by_ts) if start <= ts < min(end, limit)]
        return merge_bars(bars, start)

    # Ticks and clock

    def current(self, instrument: str, tf_name: str) -> Candle | None:
        series = self._series[instrument].get(tf_name)
        if series is None:
            return None
        bar = self._series_bar(instrument, series)
        return bar.to_candle(False) if bar else None

    def forming_m1(self, instrument: str) -> Bar | None:
        """A copy of the M1 bar that is forming, if any. It is in memory only: the database
        gets it when it closes (see the module docstring)."""
        forming = self._m1.get(instrument)
        return forming.copy() if forming is not None else None

    def on_tick(self, tick: Tick) -> None:
        name = tick.instrument
        forming = self._m1.get(name)
        if forming is not None and tick.ts >= forming.ts + M1.seconds:
            self._close_m1(name)
            forming = None
        if forming is not None and tick.ts < forming.ts:
            self.late_ticks += 1
            return
        if forming is None:
            minute = int(tick.ts) - int(tick.ts) % M1.seconds
            recent = self._recent[name]
            if recent and minute <= recent[-1].ts:
                self.late_ticks += 1  # that minute is already closed
                return
            if not recent:
                self._joined_midway.add(name)  # first minute since start or mark_gap()
            self._m1[name] = Bar.from_tick(minute, tick)
        else:
            forming.add_tick(tick)

        for series in list(self._series[name].values()):
            if tick.ts >= series.end:
                self._close_series(name, series)
                series.roll(tick.ts)
            if series.tf.kind is Kind.TICK:
                if series.bar is None:
                    series.bar = Bar.from_tick(series.start, tick)
                else:
                    series.bar.add_tick(tick)
            bar = self._series_bar(name, series)
            if bar is not None:
                self._emit(name, series.tf, bar, closed=False)

    def on_clock(self, now: float) -> None:
        """Close bars whose period ended more than `close_grace` seconds ago."""
        cutoff = now - self.close_grace
        for name, forming in list(self._m1.items()):
            if forming.ts + M1.seconds <= cutoff:
                self._close_m1(name)
        for name, by_tf in self._series.items():
            for series in by_tf.values():
                if series.end <= cutoff:
                    self._close_series(name, series)
                    series.roll(now)

    # Internals

    def _series_bar(self, name: str, series: _Series) -> Bar | None:
        if series.tf.kind is Kind.TICK:
            return series.bar
        forming = self._m1.get(name)
        if forming is not None and series.start <= forming.ts < series.end:
            if series.base is None:
                return forming.with_ts(series.start)
            return series.base.merged(forming)
        return series.base

    def _close_series(self, name: str, series: _Series) -> None:
        bar = self._series_bar(name, series)
        if bar is not None:
            self._emit(name, series.tf, bar, closed=True)
        series.base = series.bar = None

    def mark_gap(self, instrument: str) -> None:
        """The tick flow was interrupted: the next M1 bar will be partial."""
        self._m1.pop(instrument, None)
        self._recent[instrument].clear()

    def _close_m1(self, name: str) -> None:
        bar = self._m1.pop(name)
        self._recent[name].append(bar)
        if name in self._joined_midway:
            self._joined_midway.discard(name)
        elif self.on_m1_closed is not None:
            try:
                self.on_m1_closed(name, bar.to_candle(True))
            except Exception:
                log.exception("persisting M1 bar failed")
        for series in self._series[name].values():
            if series.tf.kind is Kind.TICK:
                continue
            if bar.ts >= series.end:
                self._close_series(name, series)
                series.roll(bar.ts)
            if series.start <= bar.ts < series.end:
                series.base = (
                    bar.with_ts(series.start) if series.base is None else (series.base.merged(bar))
                )
            if bar.ts + M1.seconds >= series.end:  # last minute of the period
                self._close_series(name, series)
                series.roll(series.end)

    def _emit(self, name: str, tf: Timeframe, bar: Bar, closed: bool) -> None:
        update = CandleUpdate(name, tf.name, bar.to_candle(closed), closed)
        for listener in self.listeners:
            try:
                listener(update)
            except Exception:
                log.exception("candle listener failed")
