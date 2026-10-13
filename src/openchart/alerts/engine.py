"""Alert engine (SPEC section 9): cross detection on every tick or on candle closes.

For each alert the engine keeps the previous signed distance `s_prev = price - line`
(`AlertState`). A new observation with the opposite sign is a cross: `s_prev <= 0 < s`
up, `s_prev >= 0 > s` down. A price that jumps over the line between two ticks counts.
The line is the drawing's line on the instrument's trading clock (`alerts.geometry`,
the same function the browser draws with), or a fixed level.

- `touch` alerts are checked on every tick; `candle_close` alerts when a bar of their
  timeframe closes, with that bar's last price against the line at the bar's close.
- Frequencies: `once` (then the alert is `triggered`), `once_per_bar` (at most once in
  each bar of the alert's timeframe), `repeating` (with `cooldown_s` between firings).
- Re-arming: an edited alert or a moved line forgets `s_prev`, so it needs one fresh
  observation first: a line dragged across the price is not a cross.
- Catch-up: a tick that comes after more than two minutes of trading time without
  observations (the computer slept, the feed was down, the server was stopped) opens a
  gap: live ticks are kept aside, the server repairs the stored history and `catch_up`
  replays the M1 candles of the missed period through the same checks (those fires are
  marked `late`), then the kept ticks, and the engine is live again.

The engine is synchronous and does no I/O: fires go to `fires` (an asyncio queue) and
the server persists, broadcasts and delivers them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from openchart.alerts.geometry import Clock, line_price
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import parse_timeframe
from openchart.providers.base import MID_DECIMALS, Candle, Tick
from openchart.store.alerts import Alert, AlertState
from openchart.store.drawings import Drawing

log = logging.getLogger(__name__)

# A bar is closed this long after its end even if no tick of the next bar came.
CLOSE_GRACE_SECONDS = 2.0
# Shorter pauses are covered by the next tick (a jump over the line is a cross anyway).
CATCH_UP_MIN_SECONDS = 120
CATCH_UP_MAX_SECONDS = 7 * 86400
LATE_REASONS = ("wake", "reconnect", "restart")
MAX_BUFFERED_TICKS = 20_000


def _clock_time(t: float) -> float:
    return float(t)


@dataclass
class Gap:
    start: float  # the last observation before it
    reason: str  # wake | reconnect | restart
    ticks: list[Tick]  # live ticks since, the first one revealed the gap


@dataclass
class Fire:
    alert: Alert
    ts: float  # when the cross happened
    price: float
    line_value: float | None
    cross: str  # up | down
    late: str | None = None  # wake | reconnect | restart


class AlertEngine:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        gap_seconds: float = CATCH_UP_MIN_SECONDS,
    ):
        self.now = now
        self.gap_seconds = gap_seconds
        self.alerts: dict[str, Alert] = {}
        self.lines: dict[str, Drawing] = {}
        self.clocks: dict[str, Clock] = {}
        self.gaps: dict[str, Gap] = {}  # instrument -> missed period waiting for catch_up
        # Why the next gap happened: a restart until the first live tick, "wake" after the
        # feed noticed a freeze, otherwise an outage of the price feed.
        self.next_reason = "restart"
        self.last_eval: dict[str, float] = {}  # instrument -> time of the last observation
        self.fires: asyncio.Queue[Fire] = asyncio.Queue()
        self.dirty: set[str] = set()  # alerts whose state changed since the last save
        self.changed: list[Alert] = []  # status changes (expired) for the server to save

    # Configuration

    def load(self, alerts: Iterable[Alert], drawings: Iterable[Drawing]) -> None:
        self.alerts = {a.id: a for a in alerts}
        self.lines = {d.id: d for d in drawings}

    def set_clock(self, instrument: str, clock: Clock) -> None:
        self.clocks[instrument] = clock

    def put(self, alert: Alert, *, rearm: bool = True) -> None:
        """A new or edited alert. Editing re-arms it (SPEC: re-arming)."""
        if rearm:
            alert.state = AlertState()
        self.alerts[alert.id] = alert

    def remove(self, alert_id: str) -> None:
        self.alerts.pop(alert_id, None)
        self.dirty.discard(alert_id)

    def drawing_changed(self, drawing: Drawing) -> None:
        """A line was moved: alerts on it need a fresh observation before they can fire."""
        old = self.lines.get(drawing.id)
        self.lines[drawing.id] = drawing
        if old is not None and old.points == drawing.points and old.type == drawing.type:
            return  # only the style or label changed
        for alert in self.alerts.values():
            if alert.drawing_id == drawing.id:
                alert.state = AlertState()
                self.dirty.add(alert.id)

    def drawing_removed(self, drawing_id: str) -> None:
        self.lines.pop(drawing_id, None)

    # Line value

    def line_value(self, alert: Alert, t: float) -> float | None:
        if alert.type == "level_cross":
            return alert.level
        drawing = self.lines.get(alert.drawing_id or "")
        if drawing is None:
            return None
        clock = self.clocks.get(alert.instrument, _clock_time)
        return line_price(drawing.type, drawing.points, t, clock)

    # Observations

    def on_tick(self, tick: Tick) -> None:
        """Feed listener: every live tick."""
        instrument = tick.instrument
        gap = self.gaps.get(instrument)
        if gap is not None:  # waiting for `catch_up`: keep the ticks for afterwards
            if len(gap.ticks) < MAX_BUFFERED_TICKS:
                gap.ticks.append(tick)
            return
        last = self.last_eval.get(instrument)
        if last is not None and self._gap(instrument, last, tick.ts):
            self.gaps[instrument] = Gap(last, self.next_reason, [tick])
            log.info(
                "%s: no prices since %.0fs ago (%s); catching up",
                instrument,
                tick.ts - last,
                self.next_reason,
            )
            return
        self.next_reason = "reconnect"  # live again: the next gap is an outage
        self.observe(instrument, tick.ts, tick.bid, tick.ask)

    def observe(
        self, instrument: str, t: float, bid: float, ask: float, late: str | None = None
    ) -> None:
        self.last_eval[instrument] = max(t, self.last_eval.get(instrument, 0.0))
        prices = {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, MID_DECIMALS)}
        for alert in list(self.alerts.values()):
            if alert.instrument != instrument or not alert.active:
                continue
            if alert.expires_at is not None and t >= alert.expires_at:
                continue
            price = prices[alert.trigger_price]
            if alert.trigger_mode == "touch":
                self._check(alert, t, price, late)
            else:
                self._bar_tick(alert, t, price, late)

    def _bar_tick(self, alert: Alert, t: float, price: float, late: str | None) -> None:
        st = alert.state
        start, end = bar_bounds(t, parse_timeframe(alert.timeframe))
        if st.bar_start is not None and start != st.bar_start and st.bar_price is not None:
            self._close_bar(alert, late)
        if st.bar_start != start:
            st.bar_start, st.bar_end = start, end
        st.bar_price = price
        self.dirty.add(alert.id)

    def _close_bar(self, alert: Alert, late: str | None) -> None:
        st = alert.state
        assert st.bar_end is not None and st.bar_price is not None
        end, price = st.bar_end, st.bar_price
        st.bar_start = st.bar_end = st.bar_price = None
        self._check(alert, end, price, late)

    def on_clock(self, now: float | None = None) -> None:
        """Once a second: close bars that ended without a tick after them, and expire."""
        now = self.now() if now is None else now
        for alert in list(self.alerts.values()):
            if not alert.active:
                continue
            if alert.expires_at is not None and now >= alert.expires_at:
                alert.status = "expired"
                self.changed.append(alert)
                continue
            st = alert.state
            if (
                alert.trigger_mode == "candle_close"
                and alert.instrument not in self.gaps
                and st.bar_end is not None
                and st.bar_price is not None
                and now >= st.bar_end + CLOSE_GRACE_SECONDS
            ):
                self._close_bar(alert, None)

    def _check(self, alert: Alert, t: float, price: float, late: str | None) -> None:
        st = alert.state
        value = self.line_value(alert, t)
        self.dirty.add(alert.id)
        if value is None:  # outside a segment or ray: no observation
            st.s_prev = st.t_prev = None
            return
        s = price - value
        prev, st.s_prev, st.t_prev = st.s_prev, s, t
        if prev is None:
            return  # the fresh observation after (re-)arming
        up, down = prev <= 0 < s, prev >= 0 > s
        if not (up or down):
            return
        cross = "up" if up else "down"
        if alert.direction not in ("cross", cross):
            return
        if alert.frequency == "once_per_bar":
            bar = bar_bounds(t, parse_timeframe(alert.timeframe))[0]
            if st.fired_bar == bar:
                return
            st.fired_bar = bar
        if (
            alert.cooldown_s
            and alert.last_fired_at is not None
            and t - alert.last_fired_at < alert.cooldown_s
        ):
            return
        alert.last_fired_at = int(t)
        alert.fire_count += 1
        if alert.frequency == "once":
            alert.status = "triggered"
        self.fires.put_nowait(Fire(alert, t, price, value, cross, late))

    # Catch-up after a pause

    def _gap(self, instrument: str, last: float, t: float) -> bool:
        """Missed observations: at least CATCH_UP_MIN_SECONDS since the last one, with a
        minute of trading time in between (a weekend or the daily break is no gap)."""
        if t - last < self.gap_seconds:
            return False
        clock = self.clocks.get(instrument, _clock_time)
        return clock(t) - clock(last) >= 60

    def catch_up_range(self, instrument: str) -> tuple[int, int] | None:
        """`[start, end)` of the M1 candles to replay for a pending gap: the whole minutes
        after the last observation, up to the minute of the first tick after the gap."""
        gap = self.gaps.get(instrument)
        if gap is None:
            return None
        end = int(gap.ticks[0].ts) // 60 * 60
        start = max(int(gap.start) // 60 * 60 + 60, end - CATCH_UP_MAX_SECONDS)
        return start, end

    def catch_up(self, instrument: str, candles: list[Candle]) -> int:
        """Replay the gap's M1 candles as ticks (open, then low and high in the order the
        candle most likely took, then close), then the ticks buffered meanwhile, and go
        live again. Returns the number of (late) fires from the candles."""
        gap = self.gaps.pop(instrument, None)
        if gap is None:
            return 0
        before = self.fires.qsize()
        for c in candles:
            bullish = c.mid.c >= c.mid.o
            order = ("o", "l", "h", "c") if bullish else ("o", "h", "l", "c")
            for k, field in enumerate(order):
                t = c.ts + (0, 20, 40, 59)[k]
                self.observe(
                    instrument, t, getattr(c.bid, field), getattr(c.ask, field), gap.reason
                )
        late = self.fires.qsize() - before
        for tick in gap.ticks:
            self.observe(instrument, tick.ts, tick.bid, tick.ask)
        return late

    def active(self, instrument: str | None = None) -> list[Alert]:
        return [
            a
            for a in self.alerts.values()
            if a.active and (instrument is None or a.instrument == instrument)
        ]
