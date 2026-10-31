"""A fire and the alert's state reach the database together (docs/decisions-alert-fire-atomic.md).

Each test stops the server at one moment, as a crash would: the database is copied with
SQLite's backup from a second connection, which sees only what was committed. A new server
is then started on that copy, the history of the missed minutes is stored, and the first tick
after the restart opens the catch-up. Whatever the moment, the alert log must end with each
cross exactly once: never twice, never lost.

The prices (level 1900): below until the live tick at t0+65 crosses up; above until the
minute t0+300, which closes below (a real cross down, only found by the catch-up).
"""

import asyncio
import shutil
import sqlite3
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.providers.base import OHLC, Candle
from openchart.server import Server
from openchart.store.sqlite import Database
from tests.conftest import PASSWORD, run_setup
from tests.test_alerts_api import LEVEL, push, settle


def candle(ts: int, o: float, h: float, low: float, c: float) -> Candle:
    m = OHLC(o, h, low, c)
    return Candle(ts, m, m, m, 1)


def minutes(t0: int) -> list[Candle]:
    out = [candle(t0 + 60, 1899.0, 1901.0, 1899.0, 1901.0)]  # o, l, h, c: up at t0+100
    out += [candle(t, 1901.0, 1901.2, 1900.8, 1901.0) for t in range(t0 + 120, t0 + 300, 60)]
    out.append(candle(t0 + 300, 1901.0, 1901.0, 1898.0, 1898.0))  # o, h, l, c: down at t0+340
    out += [candle(t, 1898.0, 1898.2, 1897.8, 1898.0) for t in range(t0 + 360, t0 + 600, 60)]
    return out


def snapshot(db_path, dest) -> None:
    """What a crash at this instant leaves on disk: the committed transactions only."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def start_point(harness):
    """A set-up server with a level alert that has seen the price below and saved that."""
    harness.seed_candles()
    c = TestClient(create_app(harness.server))
    c.__enter__()
    c.server = harness.server
    assert run_setup(c).status_code == 200
    created = c.post("/api/alerts", json={**LEVEL, "level": 1900.0}).json()
    t0 = int(time.time()) // 60 * 60 - 3 * 3600
    push(c, t0 + 5, 1899.0)
    c.portal.call(c.server.alerts.save_state)  # the regular save: state and last_eval
    return c, created["id"], t0


def restart_on(harness, tmp_path, snap, t0: int) -> list[dict]:
    """A new server on the crash copy; the catch-up after its first tick; the alert log."""
    data = tmp_path / "after-crash"
    skip = shutil.ignore_patterns("*.db", "*.db-wal", "*.db-shm", "server.lock")
    shutil.copytree(harness.config.data_dir, data, ignore=skip)
    config = replace(harness.config, data_dir=data)
    shutil.copyfile(snap, config.db_path)
    again = Server(config, provider=harness.provider, secrets=harness.secrets)
    with TestClient(create_app(again)) as c:
        c.server = again
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        c.portal.call(again.store.upsert_m1, "fake", "XAU_USD", minutes(t0))
        push(c, t0 + 600, 1898.0)  # the first tick after the restart reveals the gap
        for _ in range(50):
            time.sleep(0.1)
            if not again.alerts.engine.gaps and not getattr(again.alerts.engine, "unsaved", []):
                break
        settle(c)
        events = c.get("/api/alerts/events").json()
    return list(reversed(events))


def crosses(events: list[dict]) -> list[tuple[str, int, str | None]]:
    return [(e["cross"], e["ts"], e["late"]) for e in events]


def test_a_crash_between_the_event_and_the_alert_state_fires_once(harness, tmp_path):
    """Interrupted between writing the event and writing the alert's state: either both are
    on disk or neither, so the catch-up finds the cross once (here: neither, it fires late)."""
    snap = tmp_path / "crash.db"
    c, _, t0 = start_point(harness)
    seen: list[str] = []
    taken: list[str] = []

    def trace(sql: str) -> None:  # on the database thread, as each statement starts
        sql = sql.lstrip().upper()
        if (
            not taken
            and sql.startswith("UPDATE ALERTS SET")
            and any(s.startswith("INSERT INTO ALERT_EVENTS") for s in seen)
        ):
            snapshot(harness.config.db_path, snap)
            taken.append(sql)
        seen.append(sql)

    c.portal.call(c.server.db.conn.set_trace_callback, trace)
    push(c, t0 + 65, 1901.0)  # the live cross up
    settle(c)
    c.portal.call(c.server.db.conn.set_trace_callback, None)
    c.__exit__(None, None, None)
    assert taken, "the fire never wrote the alert's state"

    events = restart_on(harness, tmp_path, snap, t0)
    assert [e[0] for e in crosses(events)] == ["up", "down"]


def test_a_crash_after_the_fire_was_saved_does_not_fire_it_again(harness, tmp_path):
    """Saved fire, older last_eval (saved every 30 s): the catch-up replays minutes the alert
    already saw, and must not find the cross again, nor a cross down that never happened."""
    snap = tmp_path / "crash.db"
    c, _, t0 = start_point(harness)
    push(c, t0 + 65, 1901.0)
    settle(c)
    assert [e["cross"] for e in c.get("/api/alerts/events").json()] == ["up"]
    snapshot(harness.config.db_path, snap)
    c.__exit__(None, None, None)

    events = restart_on(harness, tmp_path, snap, t0)
    assert crosses(events) == [("up", t0 + 65, None), ("down", t0 + 340, "restart")]


def test_a_crash_before_the_fire_was_saved_does_not_lose_it(harness, tmp_path):
    """The regular save runs while a fire waits to be written: it must not save the state
    after the cross (nor a last_eval past it), or the restart would never find the cross."""
    snap = tmp_path / "crash.db"
    c, _, t0 = start_point(harness)

    async def stuck(_fire):  # the fire is never written before the crash
        await asyncio.Event().wait()

    c.server.alerts.handle = stuck
    push(c, t0 + 65, 1901.0)
    time.sleep(0.2)
    c.portal.call(c.server.alerts.save_state)
    snapshot(harness.config.db_path, snap)
    c.__exit__(None, None, None)

    events = restart_on(harness, tmp_path, snap, t0)
    assert crosses(events) == [("up", t0 + 100, "restart"), ("down", t0 + 340, "restart")]


@pytest.mark.parametrize("outer", [False, True])
async def test_atomic_writes_everything_or_nothing(tmp_path, outer):
    from openchart.store.sqlite import atomic

    async with await Database.open(tmp_path / "a.db") as db:
        await db.conn.execute("CREATE TABLE t (x INTEGER)")
        await db.conn.commit()
        if outer:  # another coroutine's statement, not committed yet
            await db.conn.execute("INSERT INTO t VALUES (0)")

        def half(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("interrupted")

        with pytest.raises(RuntimeError):
            await atomic(db.conn, half)
        await atomic(db.conn, lambda conn: conn.execute("INSERT INTO t VALUES (2)"))
        await db.conn.commit()
        rows = await db.conn.execute_fetchall("SELECT x FROM t ORDER BY x")
        assert [r[0] for r in rows] == ([0, 2] if outer else [2])
