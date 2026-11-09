"""Line and level alerts are byte-for-byte what they were before indicator alerts.

The expected bytes (`tests/golden/line_alerts_main_9f54eb1.txt`) were recorded on main at
9f54eb1 (before migration 0010 and the indicator alert routes) by running this file there
with `RECORD=1 PYTHONIOENCODING=utf-8 pytest -s`. Each alert's id, the
clock and the event's detection time are fixed, so everything a line alert writes or
answers can be compared as bytes: the REST answers, the websocket messages, and its rows
in `alerts` and `alert_events` (the columns that existed before 0010).
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import json
import os
import types
from pathlib import Path

import pytest

from openchart.providers.base import Tick
from tests.bounded import call

T = 1_791_990_000  # a fixed "now" for every timestamp the server writes
ALERT_COLUMNS = (
    "id, instrument, drawing_id, type, direction, trigger_price, trigger_mode, timeframe,"
    " frequency, cooldown_s, enabled, condition_json, state_json, created_at, name, level,"
    " expires_at, notify_json, status, updated_at, last_fired_at, fire_count"
)
EVENT_COLUMNS = (
    "id, alert_id, ts, price, message, delivered, line_value, cross, late, detected_at,"
    " attempts, error"
)
RECORD = bool(os.environ.get("RECORD"))


@pytest.fixture
def fixed(client, monkeypatch):
    ids = (f"{n:016x}" for n in itertools.count(0xA1))
    fake_time = types.SimpleNamespace(time=lambda: T)
    fake_secrets = types.SimpleNamespace(token_hex=lambda _n=8: next(ids))
    for module in ("openchart.store.alerts", "openchart.store.drawings"):
        monkeypatch.setattr(f"{module}.time", fake_time)
        monkeypatch.setattr(f"{module}.secrets", fake_secrets)
    monkeypatch.setattr(client.server.alerts, "now", lambda: T + 30)
    sent: list[str] = []
    real = client.server.hub.broadcast

    def broadcast(message):
        if message.get("type", "").startswith("alert"):
            sent.append(json.dumps(message))
        real(message)

    monkeypatch.setattr(client.server.hub, "broadcast", broadcast)
    return sent


def rows(client, sql: str) -> list[str]:
    async def read():
        conn = client.server.db.conn
        return [repr(tuple(r)) for r in await conn.execute_fetchall(sql)]

    return call(client, read)


def push(client, t: float, price: float) -> None:
    call(client, client.server.feed.handle_tick, Tick("XAU_USD", t, price - 0.1, price + 0.1))


def settle(client) -> None:
    for _ in range(5):
        call(client, client.server.alerts.dispatcher.drain)
        call(client, asyncio.sleep, 0.05)


def record_or_compare(name: str, got) -> None:
    if RECORD:
        print(f"\n{name} = {got!r}")
        return
    assert got == GOLDEN[name], name


def test_line_and_level_alerts_are_unchanged(client, fixed):
    out = {}
    assert client.put("/api/settings", json={"timezone": "UTC"}).status_code == 200
    hline ={"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    drawing = client.post("/api/drawings", json=hline).json()
    level = {"instrument": "XAU_USD", "type": "level_cross", "level": 2000.0, "frequency": "repeating"}
    line = {
        "instrument": "XAU_USD",
        "type": "line_cross",
        "drawing_id": drawing["id"],
        "direction": "up",
        "trigger_price": "bid",
        "cooldown_s": 60,
        "notify": ["app"],
    }
    out["post_level"] = client.post("/api/alerts", json=level).content
    out["post_line"] = client.post("/api/alerts", json=line).content
    level_id = json.loads(out["post_level"])["id"]
    out["put_level"] = client.put(
        f"/api/alerts/{level_id}", json={"name": "Gold 2000", "notify": ["app"]}
    ).content
    push(client, T + 10, 1999.0)
    push(client, T + 11, 2001.0)
    settle(client)
    out["list"] = client.get("/api/alerts").content
    out["list_instrument"] = client.get("/api/alerts?instrument=XAU_USD").content
    out["events"] = client.get("/api/alerts/events").content
    call(client, client.server.alerts.save_state)
    out["alert_rows"] = rows(client, f"SELECT {ALERT_COLUMNS} FROM alerts ORDER BY id")
    out["event_rows"] = rows(client, f"SELECT {EVENT_COLUMNS} FROM alert_events ORDER BY id")
    out["stopped"] = client.put(f"/api/alerts/{level_id}", json={"status": "stopped"}).content
    out["deleted"] = client.delete(f"/api/alerts/{level_id}").content
    out["broadcasts"] = list(fixed)
    for name, got in out.items():
        record_or_compare(name, got)


def _golden() -> dict:
    """`name = repr` lines, as `RECORD=1 ... -s` prints them."""
    path = Path(__file__).parent / "golden" / "line_alerts_main_9f54eb1.txt"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(" = ")
        out[name] = ast.literal_eval(value)
    return out


GOLDEN = _golden()
