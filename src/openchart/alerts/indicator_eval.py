"""Indicator alerts: evaluation on closed bars (docs/designs/indicator-alerts.md sections 2
to 5, docs/decisions-indicator-alerts.md, docs/decisions-indicator-alerts-engine.md).

- **Closed bars only.** A bar of an alert's timeframe is evaluated once it has ended
  `CLOSE_GRACE_SECONDS` ago and the instrument's prices are known up to its end (`fresh`:
  the line engine has observed, or replayed, prices up to there, or the market was closed
  since). No tick is ever evaluated.
- **One computation per group and closed bar.** A group is the alerts with the same
  instrument, timeframe, price type, definition (kind, id, version) and value inputs; any
  condition or output reads the same result.
- **Live is a replay of one bar.** Each alert keeps `last_bar` (the last closed bar it
  evaluated) and `prev`; every run evaluates, oldest first, every closed bar after
  `last_bar` up to the newest, from one computation. After a restart, a sleep, a feed
  outage or a worker outage it is the same code over more bars; those fires are `late`.
- **Arming:** the first evaluation after a create, an edit, a start or a re-arm records
  the newest closed bar and never fires.
- **The fire and the alert's state are written in one step** (`AlertStore.record_indicator_step`,
  `store.sqlite.atomic`), and the log takes one event per alert and bar.
- **Failures** (worker restarting, timed out, quarantined, the script's own error, a
  result that cannot be used) leave `last_bar` where it was and put the group's alerts in
  an error state: one message when the episode starts, one when it ends, never one per bar.
  The group is tried again after `RETRY_SECONDS`.
- **The broker's candles** may later replace a bar already evaluated (`market/feed.py`): it
  is never evaluated again and a fire already sent stands (D5); when the corrected values
  contradict a fire of the last `CORRECTION_SECONDS`, its event records them.

The evaluator takes its bars, computations and definitions through small interfaces
(`Bars`, `Compute`, `Definitions`), so the tests can give fake ones with a fake clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from openchart.alerts.engine import CATCH_UP_MAX_SECONDS, CLOSE_GRACE_SECONDS
from openchart.alerts.indicator_rules import output_exists, warnings_from_checks
from openchart.alerts.watchdog import fmt_duration
from openchart.indicators._base import Indicator
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import Timeframe, parse_timeframe
from openchart.store.alerts import Alert, AlertEvent, AlertStore, IndicatorState

log = logging.getLogger(__name__)

RETRY_SECONDS = 10.0  # a failed group is tried again after this (RETRY_AFTER_FAILED_START)
QUIET_RETRY_SECONDS = 5.0  # a traded bar not stored yet is looked for again after this
LATE_AFTER_SECONDS = 60.0  # a fire on the newest bar found later than this is late too
BEHIND_BARS = 2  # "behind": more than this many bars, and at least BEHIND_MIN_SECONDS
BEHIND_MIN_SECONDS = 300
CORRECTION_SECONDS = 86_400  # fires this recent are compared with the broker's candles
MAX_WARMUP = 20_000  # bars, as `api/routes_indicators.MAX_WARMUP` (a test keeps them equal)
COLUMNS = ["ts", "o", "h", "l", "c", "v"]
STOP_REMOVED = "the indicator was removed from the chart"


# The rules -----------------------------------------------------------------------------


def observation(a: float | None, b: float | None) -> float | None:
    """The signed difference `a - b` of one closed bar, or None (no observation) when
    either side is missing, NaN or infinite: it clears `prev`, as a ray outside its
    segment does for a line."""
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    s = a - b
    return s if math.isfinite(s) else None


def crossing(
    condition: str, signal_kind: str | None, prev: float | None, s: float | None
) -> str | None:
    """ "up", "down" or "signal" when the bar with observation `s`, after `prev`, meets the
    condition; else None. Crossings are the line rule (`engine._check`): `prev <= 0 < s`
    up, `prev >= 0 > s` down. A marker is a signal on every bar it is on; a background
    when it turns on (it was off on the bar before)."""
    if s is None:
        return None
    if condition == "signal":
        if s != 1.0:
            return None
        if signal_kind == "background":
            return "signal" if prev == 0.0 else None
        return "signal"
    if prev is None:
        return None
    if prev <= 0 < s:
        return "up"
    if prev >= 0 > s:
        return "down"
    return None


@dataclass
class Fire:
    alert: Alert
    bar: int  # start of the closed bar
    end: int  # its end: the time of the fire
    cross: str  # up | down | signal
    value: float
    other: float | None
    late: str | None
    event: AlertEvent | None = None


def advance(
    alert: Alert,
    bars: list[tuple[int, int]],
    values: list[tuple[float | None, float | None]],
    *,
    signal_kind: str | None,
    newest: int,
    now: float,
    reason: str,
) -> list[Fire]:
    """Evaluate `alert` on closed `bars` ((start, end), oldest first, ending on the newest
    closed bar) with each bar's `(output, other)` values. Changes the alert's state and
    counters and returns its fires. Bars up to `last_bar` are skipped: they were
    evaluated. Not armed: the newest bar is recorded, nothing fires."""
    st = alert.istate
    cond = alert.indicator
    assert cond is not None
    if not bars:
        return []
    obs = [_obs(cond.condition, a, b) for a, b in values]
    if not st.armed:
        st.prev, st.last_bar, st.armed = obs[-1], bars[-1][0], True
        return []
    fires: list[Fire] = []
    for (start, end), s, (a, b) in zip(bars, obs, values, strict=True):
        if st.last_bar is not None and start <= st.last_bar:
            continue
        prev = st.prev
        st.prev, st.last_bar = s, start
        if not alert.active:
            continue  # a `once` alert that fired earlier in this run: state only
        if alert.expires_at is not None and end > alert.expires_at:
            continue
        cross = crossing(cond.condition, signal_kind, prev, s)
        if cross is None or (alert.direction not in ("cross", cross) and cross != "signal"):
            continue
        if (
            alert.cooldown_s
            and alert.last_fired_at is not None
            and end - alert.last_fired_at < alert.cooldown_s
        ):
            continue
        alert.last_fired_at = end
        alert.fire_count += 1
        late = reason if (start != newest or now - end > LATE_AFTER_SECONDS) else None
        fires.append(Fire(alert, start, end, cross, float(a), _num(b), late))  # type: ignore[arg-type]
        if alert.frequency == "once":
            alert.status = "triggered"
    return fires


def _obs(condition: str, a: float | None, b: float | None) -> float | None:
    if condition == "signal":
        return observation(a, 0.0)
    return observation(a, b)


def _num(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def contradicts(
    cond_kind: str, signal_kind: str | None, prev: float | None, s: float | None, cross: str
) -> bool:
    """Whether a fire's bar, with values corrected afterwards, no longer meets it."""
    got = crossing(cond_kind, signal_kind, prev, s)
    return got != cross


# The group -----------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupKey:
    instrument: str
    timeframe: str
    price: str
    kind: str
    id: str
    digest: str
    params: str  # the value inputs as sorted JSON

    def label(self, name: str) -> str:
        return f"{name} on {self.instrument} {self.timeframe} ({self.price})"


def group_key(alert: Alert) -> GroupKey:
    c = alert.indicator
    assert c is not None
    return GroupKey(
        alert.instrument,
        alert.timeframe,
        alert.trigger_price,
        c.kind,
        c.id,
        c.digest,
        json.dumps(c.params, sort_keys=True),
    )


@dataclass
class Resolved:
    """A definition as it is now: its schema, version and how to compute it."""

    schema: Indicator | None
    digest: str | None  # the custom file's hash, "builtin"
    status: str  # ok | error | quarantined | not_run | missing
    reason: str | None = None


class Definitions(Protocol):
    async def resolve(self, kind: str, indicator_id: str) -> Resolved: ...
    async def warmup(self, key: GroupKey, schema: Indicator, settings: dict[str, Any]) -> int: ...
    def checks(self, key: GroupKey, settings: dict[str, Any]) -> dict[str, Any] | None: ...


class Bars(Protocol):
    async def read(
        self,
        instrument: str,
        tf: Timeframe,
        price: str,
        start: int | None,
        end: int,
        warm: int,
        now: float,
    ) -> pd.DataFrame: ...


Compute = Callable[
    [GroupKey, Indicator, dict[str, Any], pd.DataFrame], Awaitable[dict[str, pd.Series]]
]


class EvaluationFailed(Exception):
    """A group could not be evaluated now (the message says why)."""


@dataclass
class Episode:
    since: float
    reason: str
    late: int = 0


@dataclass
class Group:
    key: GroupKey
    retry_at: float = 0.0
    evaluated_through: int | None = None  # the newest closed bar of the last run
    ran: bool = False  # since the server started
    failing: Episode | None = None
    behind_since: float | None = None


@dataclass
class Notice:
    """What the evaluator asks the server to do: write, broadcast, send."""

    changed: list[Alert] = field(default_factory=list)
    save: list[Alert] = field(default_factory=list)  # changed outside a fire: to write
    fires: list[Fire] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    corrected: list[AlertEvent] = field(default_factory=list)


class IndicatorEvaluator:
    def __init__(
        self,
        *,
        store: AlertStore,
        alerts: Callable[[], Iterable[Alert]],
        definitions: Definitions,
        bars: Bars,
        compute: Compute,
        now: Callable[[], float] = time.time,
        fresh: Callable[[str, int], bool] = lambda _i, _t: True,
        market_open: Callable[[str, float], bool] = lambda _i, _t: True,
        wake: Callable[[], bool] = lambda: False,
        text: Callable[[Alert, AlertEvent], Awaitable[str]] | None = None,
        traded: Callable[[str, int, int], bool] = lambda _i, _s, _e: True,
    ):
        self.text = text
        self.traded = traded
        self.store = store
        self.alerts = alerts
        self.definitions = definitions
        self.bars = bars
        self.compute = compute
        self.now = now
        self.fresh = fresh
        self.market_open = market_open
        self.wake = wake
        self.groups: dict[GroupKey, Group] = {}
        self.generation: dict[str, int] = {}  # alert id -> edits seen (an edit mid-run wins)
        self.lock = asyncio.Lock()  # one run at a time; edits from the API never wait on it
        self.computations = 0  # (tests) computations started

    # Edits ---------------------------------------------------------------------------

    def touched(self, alert_id: str) -> None:
        """An alert was created, edited, started, stopped or deleted: a run in progress
        leaves it alone (its next run sees it as it is now)."""
        self.generation[alert_id] = self.generation.get(alert_id, 0) + 1

    def active(self) -> list[Alert]:
        return [
            a
            for a in self.alerts()
            if a.type == "indicator" and a.active and a.indicator is not None
        ]

    # Runs ----------------------------------------------------------------------------

    async def step(self, now: float | None = None) -> Notice:
        """Evaluate every group with a closed bar it has not evaluated. Groups of shorter
        timeframes first (1m before 1h): one bar close is one computation per group."""
        now = self.now() if now is None else now
        notice = Notice()
        async with self.lock:
            by_key: dict[GroupKey, list[Alert]] = {}
            for alert in self.active():
                by_key.setdefault(group_key(alert), []).append(alert)
            for key in list(self.groups):
                if key not in by_key and self.groups[key].failing is None:
                    del self.groups[key]
            order = sorted(
                by_key, key=lambda k: (parse_timeframe(k.timeframe).seconds, k.instrument, k.id)
            )
            for key in order:
                group = self.groups.setdefault(key, Group(key))
                await self._group(group, by_key[key], now, notice)
            await self._save(notice)
        return notice

    async def _save(self, notice: Notice) -> None:
        """Alerts changed outside a fire (an error, a stop, a re-arm), written."""
        for alert in _unique(notice.save):
            await self.store.save(alert, touch=False)
        notice.save.clear()

    async def _group(self, group: Group, alerts: list[Alert], now: float, notice: Notice) -> None:
        key = group.key
        tf = parse_timeframe(key.timeframe)
        forming = bar_bounds(now - CLOSE_GRACE_SECONDS, tf)[0]  # bars before it are over
        if now < group.retry_at:
            return
        newest_possible = self._newest_possible(key.instrument, tf, forming)
        if newest_possible is None or (
            group.failing is None
            and group.evaluated_through is not None
            and newest_possible <= group.evaluated_through
            and all(a.istate.armed for a in alerts)
        ):
            return  # nothing closed since the last run
        if (
            group.failing is None
            and group.evaluated_through is not None
            and all(a.istate.armed for a in alerts)
            and bar_bounds(newest_possible - 1, tf)[0] <= group.evaluated_through
            and not self.traded(key.instrument, newest_possible, bar_bounds(newest_possible, tf)[1])
        ):
            # The one bar that closed since the last run had no trading (the weekend).
            group.evaluated_through = newest_possible
            return
        gens = {a.id: self.generation.get(a.id, 0) for a in alerts}
        try:
            resolved = await self.definitions.resolve(key.kind, key.id)
            if resolved.status == "missing" or resolved.schema is None:
                why = resolved.reason or resolved.status
                raise EvaluationFailed(f"{key.id}: {why}")
            if key.kind == "custom" and resolved.digest != key.digest:
                # The file changed since the alerts' snapshot: re-armed or stopped first
                # (the server's `refresh`), evaluated from their next run on.
                await self._refresh(alerts, resolved, notice)
                return
            schema = resolved.schema
            settings = schema.normalize(json.loads(key.params))
            needed = int(await self.definitions.warmup(key, schema, settings))
            warm = min(max(needed, 0), MAX_WARMUP)
            start = self._first_needed(alerts, forming, tf, notice, key)
            frame = await self.bars.read(
                key.instrument, tf, key.price, start, forming, warm + 1, now
            )
            frame = self._closed(frame, tf, key.instrument)
            if frame.empty:
                group.evaluated_through = newest_possible  # no bar yet (a new instrument)
                return
            self.computations += 1
            series = await self.compute(
                key, schema, settings, frame[COLUMNS].reset_index(drop=True)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the worker, the script, a result that cannot be used
            self._failed(group, alerts, _why(exc), now, notice)
            return
        warnings = self._warnings(key, settings, needed)
        starts = [int(t) for t in frame["ts"]]
        bounds = [(s, bar_bounds(s, tf)[1]) for s in starts]
        newest = starts[-1]
        reason = self._reason(group)
        outputs = dict(zip(schema.output_ids(), schema.outputs, strict=True))
        written: list[Alert] = []
        fires: list[Fire] = []
        for alert in alerts:
            if self.generation.get(alert.id, 0) != gens[alert.id] or not alert.active:
                continue  # edited, stopped or deleted meanwhile: its next run
            cond = alert.indicator
            assert cond is not None
            try:
                values = _values(cond, outputs, settings, series, len(frame))
            except EvaluationFailed as exc:
                self._alert_failed(alert, str(exc), now, notice)
                continue
            st = alert.istate
            changed = st.error is not None or st.warnings != warnings
            st.error = st.error_since = None
            st.warnings = list(warnings)
            before = (st.last_bar, st.prev, st.armed)
            kind = outputs[cond.output].kind if cond.condition == "signal" else None
            got = advance(
                alert, bounds, values, signal_kind=kind, newest=newest, now=now, reason=reason
            )
            fires += got
            if got or changed or before != (st.last_bar, st.prev, st.armed):
                written.append(alert)
        corrected = await self._corrections(
            alerts, starts, series, outputs, settings, len(frame), now
        )
        events = [_event(f, now) for f in fires]
        for fire, event in zip(fires, events, strict=True):
            event.delivered = "telegram" not in fire.alert.notify  # nothing to send
            if self.text is not None:
                event.message = await self.text(fire.alert, event)
        # An edit that came in while this ran wins: its alerts are left to their next run.
        current = {a.id for a in written if self.generation.get(a.id, 0) == gens[a.id]}
        written = [a for a in written if a.id in current]
        kept = [(e, f) for e, f in zip(events, fires, strict=True) if f.alert.id in current]
        if written:
            stored = await self.store.record_indicator_step(
                [(e, f.alert) for e, f in kept], written
            )
            for (_e, fire), event in zip(kept, stored, strict=True):
                fire.event = event  # None: already in the log (a replay after a crash)
            notice.fires += [f for _e, f in kept if f.event is not None]
            notice.changed += written
        notice.corrected += corrected
        if group.failing is not None:
            ep = group.failing
            ep.late += sum(1 for f in fires if f.late)
            notice.messages.append(
                f"✅ Indicator alerts evaluated again: {key.label(schema.name)}, after "
                f"{fmt_duration(now - ep.since)} ({ep.reason}). "
                + (f"{ep.late} fired late." if ep.late else "None fired meanwhile.")
            )
            group.failing = None
        group.ran = True
        group.retry_at = 0.0
        group.evaluated_through = newest
        if newest < newest_possible:
            np_end = bar_bounds(newest_possible, tf)[1]
            if self.traded(key.instrument, newest_possible, np_end):
                # Its bar is not stored yet (or no price came in it): look again shortly.
                group.retry_at = now + QUIET_RETRY_SECONDS
            else:  # no trading in it (the weekend, a break): wait for the next bar
                group.evaluated_through = newest_possible

    def _newest_possible(self, instrument: str, tf: Timeframe, forming: int) -> int | None:
        """The start of the newest bar that may be closed (the one before `forming`), by
        the clock alone; None if prices are not known up to its end yet."""
        start, end = bar_bounds(forming - 1, tf)
        return start if self.fresh(instrument, end) else None

    def _first_needed(
        self, alerts: list[Alert], forming: int, tf: Timeframe, notice: Notice, key: GroupKey
    ) -> int | None:
        """The first bar to read (warmup aside): after the oldest `last_bar` of the armed
        alerts, capped at CATCH_UP_MAX_SECONDS. None: only the newest bars (all arming)."""
        armed = [
            a.istate.last_bar for a in alerts if a.istate.armed and a.istate.last_bar is not None
        ]
        if not armed:
            return None
        start = min(armed) + 1
        cap = forming - CATCH_UP_MAX_SECONDS
        if start < cap:
            skipped = [a for a in alerts if a.istate.armed and (a.istate.last_bar or 0) + 1 < cap]
            for a in skipped:  # older than the cap: not replayed; one observation first
                a.istate.last_bar, a.istate.prev = cap - 1, None
            notice.messages.append(
                f"⚠️ Indicator alerts: {len(skipped)} alert(s) on {key.instrument} {key.timeframe} "
                f"missed more than {fmt_duration(CATCH_UP_MAX_SECONDS)}; only the last "
                f"{fmt_duration(CATCH_UP_MAX_SECONDS)} were checked."
            )
            start = cap
        return start - tf.seconds  # one bar before: the D5 comparison needs its `prev`

    def _closed(self, frame: pd.DataFrame, tf: Timeframe, instrument: str) -> pd.DataFrame:
        """Complete bars whose end has fresh prices (the rest are not evaluated yet)."""
        complete = (
            frame["complete"].astype(bool).to_numpy()
            if "complete" in frame
            else np.ones(len(frame), bool)
        )
        last = len(frame) - 1  # from the newest back: the older bars are closed and fresh
        while last >= 0 and not (
            complete[last]
            and self.fresh(instrument, bar_bounds(int(frame["ts"].iloc[last]), tf)[1])
        ):
            last -= 1
        return frame.iloc[: last + 1].reset_index(drop=True)

    def _reason(self, group: Group) -> str:
        if group.failing is not None:
            return "worker"
        if not group.ran:
            return "restart"
        if self.wake():
            return "wake"
        return "reconnect"

    def _warnings(self, key: GroupKey, settings: dict[str, Any], needed: int) -> list[str]:
        out = (
            warnings_from_checks(self.definitions.checks(key, settings))
            if key.kind == "custom"
            else []
        )
        if needed > MAX_WARMUP:
            out.append("warmup_cut")
        return out

    def _failed(
        self, group: Group, alerts: list[Alert], why: str, now: float, notice: Notice
    ) -> None:
        """The group could not be evaluated: its alerts keep `last_bar` and show the
        error; one message when the episode starts (not one per bar or retry)."""
        group.retry_at = now + RETRY_SECONDS
        for alert in alerts:
            self._mark_error(alert, why, now, notice)
        if group.failing is None:
            group.failing = Episode(now, why)
            names = ", ".join(sorted({a.name or a.id for a in alerts}))
            notice.messages.append(
                f"⚠️ Indicator alerts cannot be evaluated: {group.key.instrument} "
                f"{group.key.timeframe} {group.key.id} ({len(alerts)} alert"
                f"{'s' if len(alerts) != 1 else ''}: {names}). {why}. They are checked again "
                "every few seconds and catch up on the bars missed; you get one message when "
                "they work again."
            )
        log.warning("indicator alerts %s: %s", group.key, why)

    def _mark_error(self, alert: Alert, why: str, now: float, notice: Notice) -> bool:
        """Set the alert's error; True when an episode starts for it."""
        st = alert.istate
        if st.error == why:
            return False
        started = st.error is None
        if started:
            st.error_since = int(now)
        st.error = why
        notice.changed.append(alert)
        notice.save.append(alert)
        return started

    def _alert_failed(self, alert: Alert, why: str, now: float, notice: Notice) -> None:
        """One alert of a group that was computed cannot use the result (its output is
        missing or of the wrong length): one message when its episode starts."""
        if self._mark_error(alert, why, now, notice):
            notice.messages.append(
                f"⚠️ Indicator alert cannot be evaluated: {alert.name or alert.id}. {why}."
            )

    async def _refresh(self, alerts: list[Alert], resolved: Resolved, notice: Notice) -> None:
        """The custom file changed (a new hash): each alert is re-armed on the new version,
        or stopped when its output is gone (D6)."""
        for alert in alerts:
            assert alert.indicator is not None
            stop, rearm = self.reconcile(alert, resolved, alert.indicator.params)
            if stop or rearm:
                self._apply(alert, stop, rearm, notice, why="the file was edited")

    def reconcile(
        self, alert: Alert, resolved: Resolved, params: dict[str, Any]
    ) -> tuple[str | None, bool]:
        """(stop reason, re-arm) for an alert whose instance has `params` (value inputs)
        on the definition as it is now (`resolved`). Re-arm when the version or the value
        inputs changed; stop when its output no longer fits (D6)."""
        cond = alert.indicator
        assert cond is not None
        if resolved.schema is None:
            return None, False  # cannot tell now: the run says why
        why = output_exists(resolved.schema, cond)
        if why is not None:
            return f"its output no longer fits: {why}", False
        new_digest = resolved.digest or cond.digest
        if new_digest == cond.digest and params == cond.params:
            return None, False
        cond.digest, cond.params = new_digest, dict(params)
        return None, True

    def _apply(
        self, alert: Alert, stop: str | None, rearm: bool, notice: Notice, *, why: str
    ) -> None:
        self.touched(alert.id)
        if stop is not None:
            alert.status, alert.stop_reason = "stopped", stop
            notice.messages.append(f"⏹️ Indicator alert stopped: {alert.name or alert.id}: {stop}.")
        elif rearm:
            alert.istate = IndicatorState(warnings=list(alert.istate.warnings))
            log.info("indicator alert %s re-armed: %s", alert.id, why)
        notice.changed.append(alert)
        notice.save.append(alert)

    async def instances_changed(
        self,
        instrument: str,
        instances: dict[str, tuple[Resolved, dict[str, Any]]],
    ) -> Notice:
        """The chart of `instrument` was written (`ChartIndicators.replace`): `instances`
        maps each uid now on it to its definition and value inputs. An alert whose
        instance is gone is stopped with its settings kept (D2); one whose values or file
        changed is re-armed (D6), or stopped if its output is gone. One message per
        write for the alerts it stopped because their instance was removed."""
        notice = Notice()
        removed: list[Alert] = []
        for alert in self.active():
            if alert.instrument != instrument:
                continue
            cond = alert.indicator
            assert cond is not None
            got = instances.get(cond.uid)
            if got is None:
                self.touched(alert.id)
                alert.status, alert.stop_reason = "stopped", STOP_REMOVED
                notice.changed.append(alert)
                notice.save.append(alert)
                removed.append(alert)
                continue
            resolved, params = got
            stop, rearm = self.reconcile(alert, resolved, params)
            if stop or rearm:
                self._apply(alert, stop, rearm, notice, why="its parameters or file changed")
        if removed:
            names = ", ".join(a.name or a.id for a in removed)
            many = len(removed) != 1
            notice.messages.append(
                f"⏹️ {len(removed)} indicator alert{'s' if many else ''} stopped: {names}. "
                f"The indicator was removed from the {instrument} chart; "
                + ("they keep their settings." if many else "it keeps its settings.")
            )
        await self._save(notice)
        return notice

    async def _corrections(
        self,
        alerts: list[Alert],
        starts: list[int],
        series: dict[str, pd.Series],
        outputs: dict[str, Any],
        settings: dict[str, Any],
        n: int,
        now: float,
    ) -> list[AlertEvent]:
        """D5: fires of the last day whose bar now has other values (the broker's candles
        replaced it) that no longer meet the condition. Each is recorded once, on its
        event; the fire itself stands."""
        index = {s: i for i, s in enumerate(starts)}
        by_id = {a.id: a for a in alerts}
        out = []
        since = int(now) - CORRECTION_SECONDS
        for event in await self.store.recent_indicator_events(list(by_id), since):
            i = index.get(event.bar) if event.bar is not None else None
            alert = by_id[event.alert_id]
            cond = alert.indicator
            if i is None or i == 0 or cond is None:
                continue
            try:
                values = _values(cond, outputs, settings, series, n)
            except EvaluationFailed:
                continue
            prev, s = _obs(cond.condition, *values[i - 1]), _obs(cond.condition, *values[i])
            kind = outputs[cond.output].kind if cond.condition == "signal" else None
            if not contradicts(cond.condition, kind, prev, s, event.cross):
                continue
            a, b = values[i]
            event.correction = {
                "value": _num(a),
                "other_value": _num(b),
                "at": int(now),
            }
            await self.store.set_correction(event.id, event.correction)
            log.warning(
                "indicator alert %s: the bar %s it fired on (value %s) now has %s: it would"
                " not fire on it",
                alert.id,
                event.bar,
                event.value,
                event.correction,
            )
            out.append(event)
        return out

    # The watchdog ----------------------------------------------------------------------

    def behind(self, now: float | None = None) -> list[str]:
        """Messages for groups whose alerts lag the newest closed bar by more than
        BEHIND_BARS bars (and BEHIND_MIN_SECONDS) while the market is open: one when it
        starts, one when it ends. A failing group has already said why (`_failed`)."""
        now = self.now() if now is None else now
        out = []
        by_key: dict[GroupKey, list[Alert]] = {}
        for alert in self.active():
            by_key.setdefault(group_key(alert), []).append(alert)
        for key, alerts in by_key.items():
            group = self.groups.setdefault(key, Group(key))
            tf = parse_timeframe(key.timeframe)
            forming = bar_bounds(now - CLOSE_GRACE_SECONDS, tf)[0]
            lasts = [a.istate.last_bar for a in alerts if a.istate.last_bar is not None]
            if not lasts:
                continue
            lag = forming - bar_bounds(min(lasts), tf)[1]
            late = lag > max(BEHIND_BARS * tf.seconds, BEHIND_MIN_SECONDS) and self.market_open(
                key.instrument, now
            )
            for a in alerts:
                a.istate.behind = late
            many = len(alerts) != 1
            if late and group.behind_since is None:
                group.behind_since = now - lag
                if group.failing is None:
                    out.append(
                        f"⚠️ Indicator alerts are behind: {key.instrument} {key.timeframe} "
                        f"{key.id} ({len(alerts)} alert{'s' if many else ''}) "
                        f"{fmt_duration(lag)} of closed bars not evaluated yet "
                        "(waiting for prices or the indicator worker)."
                    )
            elif not late and group.behind_since is not None:
                if group.failing is None:
                    out.append(
                        f"✅ Indicator alerts caught up: {key.instrument} {key.timeframe} "
                        f"{key.id}, after {fmt_duration(now - group.behind_since)}."
                    )
                group.behind_since = None
        return out


def _unique(alerts: list[Alert]) -> list[Alert]:
    seen: dict[str, Alert] = {}
    for a in alerts:
        seen.setdefault(a.id, a)
    return list(seen.values())


def _why(exc: BaseException) -> str:
    summary = getattr(exc, "summary", None)
    if isinstance(summary, str) and summary:
        return summary[:300]
    text = str(exc) or type(exc).__name__
    return text[:300]


def _values(
    cond: Any,
    outputs: dict[str, Any],
    settings: dict[str, Any],
    series: dict[str, pd.Series],
    n: int,
) -> list[tuple[float | None, float | None]]:
    """Each bar's (output, other) values for the condition. A result without the output,
    or without one value per bar, cannot be evaluated."""

    def column(name: str) -> np.ndarray:
        if name not in series:
            raise EvaluationFailed(f"the result has no output {name!r}")
        try:
            arr = np.asarray(pd.Series(series[name]).to_numpy(), dtype="float64")
        except (TypeError, ValueError) as exc:
            raise EvaluationFailed(f"the output {name!r} is not numbers: {exc}") from None
        if arr.shape != (n,):
            raise EvaluationFailed(f"the output {name!r} has {arr.size} values for {n} bars")
        return arr

    a = column(cond.output)
    if cond.condition == "signal":
        return [(float(x), None) for x in a]
    if cond.condition == "cross_value":
        return [(float(x), cond.value) for x in a]
    other = outputs.get(cond.other_output)
    if other is not None and other.kind == "level":
        level = settings[other.value] if isinstance(other.value, str) else other.value
        return [(float(x), float(level)) for x in a]
    b = column(cond.other_output)
    return [(float(x), float(y)) for x, y in zip(a, b, strict=True)]


def _event(f: Fire, now: float) -> AlertEvent:
    return AlertEvent(
        id=0,
        alert_id=f.alert.id,
        ts=f.end,
        price=f.value,
        line_value=f.other,
        cross=f.cross,
        message="",
        late=f.late,
        detected_at=int(now),
        bar=f.bar,
        value=f.value,
        other_value=f.other,
    )
