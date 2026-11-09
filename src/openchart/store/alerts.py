"""Alerts and their event log (SPEC section 9).

An alert watches one instrument: a drawn line (`line_cross`, `drawing_id`), a price
level (`level_cross`, `level`) or an indicator on its chart (`indicator`: the instance and
condition in `condition_json`, `IndicatorCondition`). Its evaluation state (the previous
signed distance and so on) lives in `state_json`, so a restart continues where it stopped:
`AlertState` for lines and levels, `IndicatorState` for indicators.

Line and level alerts read and write exactly what they did before indicator alerts
(`tests/test_line_alerts_unchanged.py`): the indicator fields are only in an indicator
alert's JSON, and are NULL in a line alert's row.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiosqlite

from openchart.store.sqlite import atomic

TYPES = ("line_cross", "level_cross", "indicator")
DIRECTIONS = ("cross", "up", "down")
PRICES = ("bid", "ask", "mid")
MODES = ("touch", "candle_close")
FREQUENCIES = ("once", "once_per_bar", "repeating")
STATUSES = ("active", "triggered", "expired", "stopped")
CHANNELS = ("telegram", "app")
CONDITIONS = ("cross_value", "cross_output", "signal")
# Shown on an indicator alert (docs/designs/indicator-alerts.md section 4): the script was
# found to look ahead, its declared warmup was found short, it was not checked (yet), or
# its warmup is longer than the bars the server reads.
WARNINGS = ("lookahead", "warmup_short", "unchecked", "warmup_cut")


@dataclass
class AlertState:
    """What the engine remembers between observations."""

    s_prev: float | None = None  # previous signed distance price - line
    t_prev: float | None = None
    bar_start: int | None = None  # candle_close: the bar being built
    bar_end: int | None = None
    bar_price: float | None = None  # candle_close: last price seen in that bar
    fired_bar: int | None = None  # once_per_bar: start of the bar it last fired in
    seen_through: float | None = None  # time of the last observation (a catch-up skips it)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> AlertState:
        data = data or {}
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


@dataclass
class IndicatorCondition:
    """What an indicator alert watches: one instance on the chart of its instrument, as it
    was when the alert was saved, and the condition on its outputs."""

    uid: str  # the instance (chart_indicators.uid)
    kind: str  # builtin | custom
    id: str  # the definition's id
    params: dict[str, Any]  # its value inputs (style inputs dropped)
    digest: str  # the custom file's hash, or "builtin"
    output: str
    condition: str  # cross_value | cross_output | signal
    other_output: str | None = None  # cross_output
    value: float | None = None  # cross_value
    name: str = ""  # the indicator's short name, for messages

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> IndicatorCondition | None:
        """None for a condition that is not complete (written by hand or by an older
        version): such an alert is listed and can be deleted, but never evaluated."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except TypeError:
            return None


@dataclass
class IndicatorState:
    """What the indicator evaluation remembers between closed bars."""

    last_bar: int | None = None  # start of the last closed bar evaluated
    prev: float | None = None  # the previous bar's difference (crossings) or 0/1 (background)
    armed: bool = False  # False: the next evaluation records the state and never fires
    error: str | None = None  # why it cannot be evaluated now (worker, script), or None
    error_since: int | None = None  # start of that failure episode
    warnings: list[str] = field(default_factory=list)
    behind: bool = False  # its last bar lags the newest closed one (the watchdog's view)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> IndicatorState:
        data = data or {}
        state = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        state.warnings = [w for w in state.warnings or [] if w in WARNINGS]
        return state


@dataclass
class Alert:
    id: str
    instrument: str
    type: str
    direction: str = "cross"
    trigger_price: str = "mid"
    trigger_mode: str = "touch"
    timeframe: str = "1m"
    frequency: str = "once"
    cooldown_s: int = 0
    drawing_id: str | None = None
    level: float | None = None
    name: str = ""
    expires_at: int | None = None
    notify: list[str] = field(default_factory=lambda: ["telegram", "app"])
    status: str = "active"
    created_at: int = 0
    updated_at: int = 0
    last_fired_at: int | None = None
    fire_count: int = 0
    state: AlertState = field(default_factory=AlertState)
    # Indicator alerts only (None and unused on line and level alerts):
    indicator: IndicatorCondition | None = None
    stop_reason: str | None = None
    istate: IndicatorState = field(default_factory=IndicatorState)

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instrument": self.instrument,
            "type": self.type,
            "drawing_id": self.drawing_id,
            "level": self.level,
            "direction": self.direction,
            "trigger_price": self.trigger_price,
            "trigger_mode": self.trigger_mode,
            "timeframe": self.timeframe,
            "frequency": self.frequency,
            "cooldown_s": self.cooldown_s,
            "name": self.name,
            "expires_at": self.expires_at,
            "notify": self.notify,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_fired_at": self.last_fired_at,
            "fire_count": self.fire_count,
            **(self._indicator_json() if self.type == "indicator" else {}),
        }

    def _indicator_json(self) -> dict[str, Any]:
        st = self.istate
        return {
            "indicator": self.indicator.to_json() if self.indicator else None,
            "stop_reason": self.stop_reason,
            "state": {
                "last_bar": st.last_bar,
                "armed": st.armed,
                "error": st.error,
                "error_since": st.error_since,
                "warnings": list(st.warnings),
                "behind": st.behind,
            },
        }

    def state_json(self) -> str:
        if self.type == "indicator":
            return json.dumps(self.istate.to_json())
        return json.dumps(self.state.to_json())

    def condition_json(self) -> str | None:
        return json.dumps(self.indicator.to_json()) if self.indicator is not None else None


@dataclass
class AlertEvent:
    id: int
    alert_id: str
    ts: int  # when the cross happened
    price: float
    line_value: float | None
    cross: str
    message: str
    late: str | None = None  # wake | reconnect | restart: found from history afterwards
    detected_at: int = 0
    delivered: bool = False
    attempts: int = 0
    error: str | None = None
    # Indicator alerts only (absent from a line alert's JSON):
    bar: int | None = None  # start of the closed bar it fired on
    value: float | None = None
    other_value: float | None = None
    correction: dict[str, Any] | None = None  # the broker's candles contradicted it (D5)

    def to_json(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "alert_id": self.alert_id,
            "ts": self.ts,
            "price": self.price,
            "line_value": self.line_value,
            "cross": self.cross,
            "message": self.message,
            "late": self.late,
            "detected_at": self.detected_at,
            "delivered": self.delivered,
            "attempts": self.attempts,
            "error": self.error,
        }
        if self.bar is not None:
            out |= {
                "bar": self.bar,
                "value": self.value,
                "other_value": self.other_value,
                "correction": self.correction,
            }
        return out


_COLUMNS = (
    "id, instrument, type, direction, trigger_price, trigger_mode, timeframe, frequency,"
    " cooldown_s, drawing_id, level, name, expires_at, notify_json, status, created_at,"
    " updated_at, last_fired_at, fire_count, state_json, stop_reason, condition_json"
)


def _alert(r: tuple) -> Alert:
    state = json.loads(r[19]) if r[19] else None
    if r[2] == "indicator":
        return Alert(
            **_common(r),
            state=AlertState(),
            indicator=IndicatorCondition.from_json(json.loads(r[21])) if r[21] else None,
            stop_reason=r[20],
            istate=IndicatorState.from_json(state),
        )
    return Alert(**_common(r), state=AlertState.from_json(state))


def _common(r: tuple) -> dict[str, Any]:
    return dict(
        id=r[0],
        instrument=r[1],
        type=r[2],
        direction=r[3],
        trigger_price=r[4],
        trigger_mode=r[5],
        timeframe=r[6] or "1m",
        frequency=r[7],
        cooldown_s=r[8] or 0,
        drawing_id=r[9],
        level=r[10],
        name=r[11] or "",
        expires_at=r[12],
        notify=json.loads(r[13]) if r[13] else ["telegram", "app"],
        status=r[14],
        created_at=r[15],
        updated_at=r[16],
        last_fired_at=r[17],
        fire_count=r[18] or 0,
    )


_EVENT_COLUMNS = (
    "id, alert_id, ts, price, line_value, cross, message, late, detected_at, delivered,"
    " attempts, error, bar, value, other_value, correction_json"
)


def _event(r: tuple) -> AlertEvent:
    return AlertEvent(
        id=r[0],
        alert_id=r[1],
        ts=r[2],
        price=r[3],
        line_value=r[4],
        cross=r[5] or "",
        message=r[6] or "",
        late=r[7],
        detected_at=r[8] or r[2],
        delivered=bool(r[9]),
        attempts=r[10] or 0,
        error=r[11],
        bar=r[12],
        value=r[13],
        other_value=r[14],
        correction=json.loads(r[15]) if r[15] else None,
    )


class AlertStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def list(self, instrument: str | None = None) -> list[Alert]:
        sql = (
            f"SELECT {_COLUMNS} FROM alerts WHERE (? IS NULL OR instrument=?)"
            " ORDER BY created_at, id"
        )
        async with self.conn.execute(sql, (instrument, instrument)) as cur:
            return [_alert(r) for r in await cur.fetchall()]

    async def get(self, alert_id: str) -> Alert | None:
        async with self.conn.execute(
            f"SELECT {_COLUMNS} FROM alerts WHERE id=?", (alert_id,)
        ) as cur:
            row = await cur.fetchone()
        return _alert(row) if row else None

    async def create(self, alert: Alert) -> Alert:
        now = int(time.time())
        alert.id = alert.id or secrets.token_hex(8)
        alert.created_at = alert.updated_at = now
        await self.conn.execute(
            f"INSERT INTO alerts ({_COLUMNS}, enabled) VALUES"
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*self._values(alert), int(alert.active)),
        )
        await self.conn.commit()
        return alert

    async def save(self, alert: Alert, *, touch: bool = True) -> Alert:
        """Write every field. `touch`: a user edit (bumps `updated_at`)."""
        if touch:
            alert.updated_at = int(time.time())
        values = self._values(alert)
        await self.conn.execute(
            "UPDATE alerts SET instrument=?, type=?, direction=?, trigger_price=?,"
            " trigger_mode=?, timeframe=?, frequency=?, cooldown_s=?, drawing_id=?, level=?,"
            " name=?, expires_at=?, notify_json=?, status=?, created_at=?, updated_at=?,"
            " last_fired_at=?, fire_count=?, state_json=?, stop_reason=?, condition_json=?,"
            " enabled=? WHERE id=?",
            (*values[1:], int(alert.active), alert.id),
        )
        await self.conn.commit()
        return alert

    async def save_states(self, alerts: list[Alert]) -> None:
        await self.conn.executemany(
            "UPDATE alerts SET state_json=? WHERE id=?",
            [(a.state_json(), a.id) for a in alerts],
        )
        await self.conn.commit()

    async def delete(self, alert_id: str) -> Alert | None:
        alert = await self.get(alert_id)
        if alert is not None:
            await self.conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            await self.conn.commit()
        return alert

    async def for_drawing(self, drawing_id: str) -> list[Alert]:
        return [a for a in await self.list() if a.drawing_id == drawing_id]

    async def for_instance(self, instrument: str, uid: str) -> list[Alert]:
        """Indicator alerts on one instance of the instrument's chart."""
        return [
            a
            for a in await self.list(instrument)
            if a.type == "indicator" and a.indicator is not None and a.indicator.uid == uid
        ]

    @staticmethod
    def _values(a: Alert) -> tuple:
        return (
            a.id,
            a.instrument,
            a.type,
            a.direction,
            a.trigger_price,
            a.trigger_mode,
            a.timeframe,
            a.frequency,
            a.cooldown_s,
            a.drawing_id,
            a.level,
            a.name,
            a.expires_at,
            json.dumps(a.notify),
            a.status,
            a.created_at,
            a.updated_at,
            a.last_fired_at,
            a.fire_count,
            a.state_json(),
            a.stop_reason,
            a.condition_json(),
        )

    # Events

    async def add_event(self, event: AlertEvent) -> AlertEvent:
        cur = await self.conn.execute(
            "INSERT INTO alert_events (alert_id, ts, price, line_value, cross, message, late,"
            " detected_at, delivered, attempts, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.alert_id,
                event.ts,
                event.price,
                event.line_value,
                event.cross,
                event.message,
                event.late,
                event.detected_at,
                int(event.delivered),
                event.attempts,
                event.error,
            ),
        )
        await self.conn.commit()
        event.id = cur.lastrowid or 0
        return event

    async def record_fire(
        self,
        event: AlertEvent,
        alert: Alert,
        state: AlertState,
        fire_count: int,
        last_fired_at: int | None,
    ) -> AlertEvent:
        """A fire: its event and the alert's state right after it, both or neither
        (docs/decisions-alert-fire-atomic.md). The status is the alert's now."""

        def write(db) -> int:
            cur = db.execute(
                "INSERT INTO alert_events (alert_id, ts, price, line_value, cross, message,"
                " late, detected_at, delivered, attempts, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.alert_id,
                    event.ts,
                    event.price,
                    event.line_value,
                    event.cross,
                    event.message,
                    event.late,
                    event.detected_at,
                    int(event.delivered),
                    event.attempts,
                    event.error,
                ),
            )
            db.execute(
                "UPDATE alerts SET status=?, enabled=?, fire_count=?, last_fired_at=?,"
                " state_json=? WHERE id=?",
                (
                    alert.status,
                    int(alert.active),
                    fire_count,
                    last_fired_at,
                    json.dumps(state.to_json()),
                    alert.id,
                ),
            )
            return cur.lastrowid or 0

        event.id = await atomic(self.conn, write)
        return event

    async def record_indicator_step(
        self, fires: list[tuple[AlertEvent, Alert]], alerts: list[Alert]
    ) -> list[AlertEvent | None]:
        """One evaluation of indicator alerts: their fires' events and every alert's state
        (and counters and status) right after it, all or nothing (`atomic`). An event
        already in the log for that alert and bar (a replay after a crash) is not written
        again: its entry is None, and nothing is sent for it."""

        def write(db) -> list[int | None]:
            ids: list[int | None] = []
            for event, _alert in fires:
                cur = db.execute(
                    "INSERT OR IGNORE INTO alert_events (alert_id, ts, price, line_value, cross,"
                    " message, late, detected_at, delivered, attempts, error, bar, value,"
                    " other_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.alert_id,
                        event.ts,
                        event.price,
                        event.line_value,
                        event.cross,
                        event.message,
                        event.late,
                        event.detected_at,
                        int(event.delivered),
                        event.attempts,
                        event.error,
                        event.bar,
                        event.value,
                        event.other_value,
                    ),
                )
                ids.append(cur.lastrowid if cur.rowcount == 1 else None)
            for alert in alerts:
                db.execute(
                    "UPDATE alerts SET status=?, enabled=?, fire_count=?, last_fired_at=?,"
                    " state_json=?, stop_reason=? WHERE id=?",
                    (
                        alert.status,
                        int(alert.active),
                        alert.fire_count,
                        alert.last_fired_at,
                        alert.state_json(),
                        alert.stop_reason,
                        alert.id,
                    ),
                )
            return ids

        ids = await atomic(self.conn, write)
        out: list[AlertEvent | None] = []
        for (event, _alert), event_id in zip(fires, ids, strict=True):
            if event_id is None:
                out.append(None)
            else:
                event.id = event_id
                out.append(event)
        return out

    async def recent_indicator_events(self, alert_ids: list[str], since: int) -> list[AlertEvent]:
        """Fires of these indicator alerts on bars since `since` not yet found contradicted
        by the broker's candles (D5)."""
        if not alert_ids:
            return []
        marks = ", ".join("?" * len(alert_ids))
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM alert_events WHERE alert_id IN ({marks})"
            " AND bar IS NOT NULL AND bar >= ? AND correction_json IS NULL ORDER BY id"
        )
        async with self.conn.execute(sql, (*alert_ids, since)) as cur:
            return [_event(r) for r in await cur.fetchall()]

    async def set_correction(self, event_id: int, correction: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE alert_events SET correction_json=? WHERE id=?",
            (json.dumps(correction), event_id),
        )
        await self.conn.commit()

    async def events(self, limit: int = 100, alert_id: str | None = None) -> list[AlertEvent]:
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM alert_events WHERE (? IS NULL OR alert_id=?)"
            " ORDER BY id DESC LIMIT ?"
        )
        async with self.conn.execute(sql, (alert_id, alert_id, limit)) as cur:
            return [_event(r) for r in await cur.fetchall()]

    async def event(self, event_id: int) -> AlertEvent | None:
        async with self.conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM alert_events WHERE id=?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
        return _event(row) if row else None

    async def set_delivery(
        self, event_id: int, delivered: bool, attempts: int, error: str | None
    ) -> None:
        await self.conn.execute(
            "UPDATE alert_events SET delivered=?, attempts=?, error=? WHERE id=?",
            (int(delivered), attempts, error, event_id),
        )
        await self.conn.commit()

    async def undelivered(self, since: int) -> list[AlertEvent]:
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM alert_events WHERE delivered=0 AND detected_at>=?"
            " ORDER BY id"
        )
        async with self.conn.execute(sql, (since,)) as cur:
            return [_event(r) for r in await cur.fetchall()]
