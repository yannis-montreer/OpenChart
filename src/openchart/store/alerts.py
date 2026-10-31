"""Alerts and their event log (SPEC section 9).

An alert watches one instrument: a drawn line (`line_cross`, `drawing_id`) or a price
level (`level_cross`, `level`). Its evaluation state (the previous signed distance and
so on) lives in `state_json`, so a restart continues where it stopped.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiosqlite

from openchart.store.sqlite import atomic

TYPES = ("line_cross", "level_cross")
DIRECTIONS = ("cross", "up", "down")
PRICES = ("bid", "ask", "mid")
MODES = ("touch", "candle_close")
FREQUENCIES = ("once", "once_per_bar", "repeating")
STATUSES = ("active", "triggered", "expired", "stopped")
CHANNELS = ("telegram", "app")


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
        }


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

    def to_json(self) -> dict[str, Any]:
        return {
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


_COLUMNS = (
    "id, instrument, type, direction, trigger_price, trigger_mode, timeframe, frequency,"
    " cooldown_s, drawing_id, level, name, expires_at, notify_json, status, created_at,"
    " updated_at, last_fired_at, fire_count, state_json"
)


def _alert(r: tuple) -> Alert:
    return Alert(
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
        state=AlertState.from_json(json.loads(r[19]) if r[19] else None),
    )


_EVENT_COLUMNS = (
    "id, alert_id, ts, price, line_value, cross, message, late, detected_at, delivered,"
    " attempts, error"
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
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            " last_fired_at=?, fire_count=?, state_json=?, enabled=? WHERE id=?",
            (*values[1:], int(alert.active), alert.id),
        )
        await self.conn.commit()
        return alert

    async def save_states(self, alerts: list[Alert]) -> None:
        await self.conn.executemany(
            "UPDATE alerts SET state_json=? WHERE id=?",
            [(json.dumps(a.state.to_json()), a.id) for a in alerts],
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
            json.dumps(a.state.to_json()),
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
