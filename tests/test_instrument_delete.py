"""Deleting a removed instrument's data from Settings (docs/decisions-instrument-delete.md).

A preview first says what would go and hands out a token; the delete needs that token, so
a preview taken before newer data arrived cannot delete it. Refused for an enabled
instrument, one the feed still downloads, one whose lock is held (a repair, an add-back),
and one with drawings or alerts unless the request names them. Nothing outside that
instrument changes. `openchart purge` and the server share the deletion
(`openchart.purge.delete_instrument`).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace

import pytest

from openchart import purge
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Tick
from openchart.store.alerts import AlertEvent
from tests.bounded import call
from tests.conftest import GOLD
from tests.test_instrument_removal import remove
from tests.test_instruments import add, instruments, market  # noqa: F401  (the fixture)

E = "EUR_USD"
G = GOLD.instrument


def preview(client, name: str = E, status: int = 200) -> dict:
    r = client.get(f"/api/instruments/{name}/delete-preview")
    assert r.status_code == status, r.text
    return r.json()


def delete(client, token: str, name: str = E, *, confirm: str | None = None, both=False):
    body = {"token": token, "confirm": name if confirm is None else confirm}
    if both:
        body["drawings_and_alerts"] = True
    return client.post(f"/api/instruments/{name}/delete", json=body)


def line_and_alert(client, name: str = E, price: float = 1.08) -> tuple[dict, dict]:
    drawing = client.post(
        "/api/drawings",
        json={"instrument": name, "type": "hline", "points": [{"t": 1, "p": price}]},
    ).json()
    alert = client.post(
        "/api/alerts", json={"type": "line_cross", "instrument": name, "drawing_id": drawing["id"]}
    ).json()
    return drawing, alert


def tick_file(client, name: str, day: str = "2026-10-01", data: bytes = b"PAR1-fake") -> None:
    folder = client.server.config.ticks_dir / "fake" / name / f"date={day}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "part-0.parquet").write_bytes(data)


def counts(client, name: str = E) -> tuple[int, int, int]:
    """(M1, H1, cached 4h/D/W bars) stored for `name`."""
    return tuple(call(client, client.server.store.row_counts, "fake", name)[:3])


@pytest.fixture
def removed(market):  # noqa: F811
    """EUR_USD downloaded, its daily bars cached, a tick file stored, then removed."""
    client = market
    add(client, E)
    assert client.get(f"/api/candles?instrument={E}&timeframe=1D&limit=50").status_code == 200
    tick_file(client, E)
    remove(client, E)
    return client


# The preview


def test_the_preview_says_what_would_go_and_deletes_nothing(removed):
    client = removed
    line_and_alert(client)
    before = counts(client)
    p = preview(client)
    s = p["stored"]
    assert (s["m1"], s["h1"], s["cached"]) == before and all(before)
    assert s["tick_files"] == 1 and s["tick_bytes"] == len(b"PAR1-fake")
    assert s["bytes"] > s["tick_bytes"]
    assert (s["drawings"], s["alerts"]) == (1, 1)
    assert p["needs_drawings_and_alerts"] is True
    assert isinstance(p["token"], str) and len(p["token"]) >= 32
    assert counts(client) == before
    assert preview(client)["token"] == p["token"]  # nothing changed: the same token


def test_an_unknown_instrument_is_404(removed):
    preview(removed, "NOPE_USD", 404)
    assert delete(removed, "x" * 64, "NOPE_USD").status_code == 404


def test_login_and_the_same_origin_check_apply(removed):
    client = removed
    token = preview(client)["token"]
    cross = client.post(
        f"/api/instruments/{E}/delete",
        json={"token": token, "confirm": E},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    client.post("/api/logout")
    assert client.get(f"/api/instruments/{E}/delete-preview").status_code == 401
    assert delete(client, token).status_code == 401
    assert all(counts(client))


# The delete


def test_the_delete_removes_the_instrument_its_candles_cache_closures_and_ticks(removed):
    client = removed
    server = client.server
    sid = call(client, server.store.series, "fake", E).id
    r = delete(client, preview(client)["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"]["candles_m1"] > 0 and body["deleted"]["instruments"] == 1
    assert body["vacuum"]["before"] > 0 and body["vacuum"]["after"] > 0
    assert E not in instruments(client)
    assert counts(client) == (0, 0, 0)
    assert not (server.config.ticks_dir / "fake" / E).exists()

    async def leftovers():
        conn = server.store.conn
        n = 0
        for table in ("candles_m1", "candles_h1", "candles_cache"):
            n += (
                await conn.execute_fetchall(f"SELECT COUNT(*) FROM {table} WHERE series=?", (sid,))
            )[0][0]
        for table in ("series", "closures", "cache_dirty", "instruments"):
            sql = f"SELECT COUNT(*) FROM {table} WHERE instrument=?"
            n += (await conn.execute_fetchall(sql, (E,)))[0][0]
        return n

    assert call(client, leftovers) == 0
    assert G in instruments(client)


def test_it_can_be_added_again_afresh(removed):
    client = removed
    assert delete(client, preview(client)["token"]).status_code == 200
    add(client, E)
    assert instruments(client)[E]["enabled"] is True
    assert counts(client)[0] > 0


def test_the_typed_name_must_match(removed):
    client = removed
    token = preview(client)["token"]
    for wrong in ("", "eur_usd", "EUR", "EUR_USD "):
        r = delete(client, token, confirm=wrong)
        assert r.status_code == 400, (wrong, r.text)
    assert all(counts(client))


# The refusals


def test_an_enabled_instrument_is_refused(market):  # noqa: F811
    client = market
    add(client, E)
    r = client.get(f"/api/instruments/{E}/delete-preview")
    assert r.status_code == 409 and "enabled" in r.json()["detail"]
    before = counts(client)
    r = delete(client, "0" * 64)
    assert r.status_code == 409 and "enabled" in r.json()["detail"]
    assert counts(client) == before


def test_drawings_and_alerts_must_be_named(removed):
    client = removed
    drawing, alert = line_and_alert(client)
    token = preview(client)["token"]
    r = delete(client, token)
    assert r.status_code == 409 and "drawing" in r.json()["detail"]
    assert all(counts(client))
    assert [d["id"] for d in client.get(f"/api/drawings?instrument={E}").json()] == [drawing["id"]]

    r = delete(client, token, both=True)
    assert r.status_code == 200, r.text
    assert (r.json()["drawings"], r.json()["alerts"]) == (1, 1)
    assert client.get(f"/api/drawings?instrument={E}").json() == []
    assert alert["id"] not in {a["id"] for a in client.get("/api/alerts").json()}
    engine = client.server.alerts.engine
    assert alert["id"] not in engine.alerts and drawing["id"] not in engine.lines


def test_without_drawings_or_alerts_nothing_has_to_be_named(removed):
    client = removed
    p = preview(client)
    assert p["needs_drawings_and_alerts"] is False
    assert delete(client, p["token"]).status_code == 200


def test_kept_drawings_of_other_instruments_do_not_block(removed):
    client = removed
    line_and_alert(client, G, 2650.0)
    assert delete(client, preview(client)["token"]).status_code == 200


def gated_download(client):
    """EUR_USD's history download waits in the fake broker until `release()`."""
    provider = client.server.provider
    released = False
    original = provider.fetch_page_before

    async def slow(*args, **kwargs):
        while not released:  # noqa: ASYNC110 (set from the test's thread, not the loop)
            await asyncio.sleep(0.01)
        return await original(*args, **kwargs)

    provider.fetch_page_before = slow

    def release():
        nonlocal released
        released = True
        provider.fetch_page_before = original

    return release


def test_a_delete_during_a_download_is_refused(market):  # noqa: F811
    """The window between the row turning disabled and the feed letting the instrument go
    (a remove in progress): the feed still downloads it, so the delete is refused."""
    client = market
    release = gated_download(client)
    try:
        assert client.post("/api/instruments", json={"instrument": E}).status_code == 200
        deadline = time.time() + 10
        while E not in client.server.feed.downloading:
            assert time.time() < deadline
            time.sleep(0.02)
        assert call(client, client.server.store.set_instrument_enabled, "fake", E, False)
        r = client.get(f"/api/instruments/{E}/delete-preview")
        assert r.status_code == 409 and "download" in r.json()["detail"], r.text
        r = delete(client, "0" * 64)
        assert r.status_code == 409 and "download" in r.json()["detail"], r.text
        assert call(client, client.server.store.instrument, "fake", E) is not None
    finally:
        release()


def test_a_held_instrument_lock_refuses_the_delete(removed):
    """A repair before an alert catch-up, an add-back or a minute-history change holds the
    instrument's lock; a delete then is refused with what holds it, not queued."""
    client = removed
    server = client.server
    token = preview(client)["token"]
    entered, leave = asyncio.Event(), asyncio.Event()

    async def hold():
        async with server.instrument_locks.hold(E, "repair before the alert catch-up"):
            entered.set()
            await leave.wait()

    task = client.portal.start_task_soon(hold)
    call(client, entered.wait)
    try:
        r = client.get(f"/api/instruments/{E}/delete-preview")
        assert r.status_code == 409 and "repair" in r.json()["detail"]
        r = delete(client, token)
        assert r.status_code == 409 and "repair" in r.json()["detail"]
    finally:
        client.portal.call(leave.set)
        task.result(timeout=10)
    assert all(counts(client))
    assert delete(client, token).status_code == 200


def test_the_alert_catch_up_repairs_only_what_the_feed_serves_under_the_lock(market):  # noqa: F811
    client = market
    server = client.server
    add(client, E)
    remove(client, E)
    seen = []

    async def repair_instrument(name):
        seen.append((name, server.instrument_locks.busy(name)))

    server.feed.repair_instrument = repair_instrument
    engine = server.alerts.engine
    for name in (E, G):
        engine.gaps[name] = type("Gap", (), {"reason": "reconnect"})()
        engine.catch_up_range = lambda instrument: None
        engine.catch_up = lambda instrument, candles: 0
        call(client, server.alerts.catch_up, name)
    assert set(seen) == {(G, "repair before the alert catch-up")}  # never the removed one


# The preview token


def test_a_stale_preview_cannot_delete_newer_data(removed):
    client = removed
    server = client.server
    newer = replace(client.euro[-1], ts=client.euro[-1].ts + 60)
    line = {"instrument": E, "type": "hline", "points": [{"t": 1, "p": 1.07}]}
    changes = {
        "a drawing alone": lambda: client.post("/api/drawings", json=line),
        "a drawing and an alert": lambda: line_and_alert(client),
        "a tick file": lambda: tick_file(client, E, "2026-10-02"),
        "a newer minute": lambda: call(client, server.store.upsert_m1, "fake", E, [newer]),
    }
    for what, change in changes.items():
        token = preview(client)["token"]
        change()
        assert preview(client)["token"] != token, what
    token = preview(client)["token"]
    line_and_alert(client)
    before = counts(client)
    r = delete(client, token, both=True)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert counts(client) == before
    assert delete(client, "f" * 64, both=True).status_code == 409
    assert client.post(f"/api/instruments/{E}/delete", json={"confirm": E}).status_code == 422
    assert delete(client, preview(client)["token"], both=True).status_code == 200


# Nothing else changes


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _everything(server, name: str, sid: int, alert_ids: set[str]) -> dict:
    """Every row of every table, minus the rows of `name`, and every tick file outside
    its folder, by content."""
    conn = server.store.conn
    tables = [
        r[0]
        for r in await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    out: dict = {}
    for table in sorted(tables):
        cols = [r[1] for r in await conn.execute_fetchall(f"PRAGMA table_info({table})")]
        rows = []
        for row in await conn.execute_fetchall(f"SELECT * FROM {table}"):
            values = dict(zip(cols, row, strict=True))
            if values.get("instrument") == name or values.get("series") == sid:
                continue
            if table == "series" and values.get("id") == sid:
                continue
            if table == "alert_events" and values.get("alert_id") in alert_ids:
                continue
            rows.append(repr(row))
        out[table] = sorted(rows)
    ticks = server.config.ticks_dir
    out["files"] = sorted(
        (str(p.relative_to(ticks)), _digest(p))
        for p in ticks.rglob("*")
        if p.is_file() and (ticks / "fake" / name) not in p.parents
    )
    return out


def test_nothing_outside_the_instrument_changes(removed):
    client = removed
    server = client.server
    line_and_alert(client, G, 2650.0)
    level = client.post(
        "/api/alerts", json={"type": "level_cross", "instrument": G, "level": 2651.0}
    )
    assert level.status_code == 200, level.text
    _, euro_alert = line_and_alert(client)
    tick_file(client, G, data=b"gold ticks")
    assert client.put("/api/settings", json={"timezone": "Europe/Paris"}).status_code == 200
    layout = client.post("/api/layouts", json={"name": "Gold", "layout": {"instrument": G}})
    assert layout.status_code in (200, 201), layout.text
    assert client.get(f"/api/candles?instrument={G}&timeframe=1W&limit=50").status_code == 200
    for alert_id in (level.json()["id"], euro_alert["id"]):  # an alert log on both sides
        event = AlertEvent(0, alert_id, 1, 1.0, None, "up", "fired", detected_at=1)
        call(client, server.alerts.store.add_event, event)
    sid = call(client, server.store.series, "fake", E).id
    ids = {euro_alert["id"]}
    before = call(client, _everything, server, E, sid, ids)
    assert before["candles_m1"] and before["drawings"] and before["alerts"] and before["files"]
    assert before["alert_events"]

    assert delete(client, preview(client)["token"], both=True).status_code == 200
    after = call(client, _everything, server, E, sid, ids)
    for table in before:
        assert after[table] == before[table], table


# Concurrency


def test_a_delete_with_the_feed_running_leaves_nothing_behind(removed):
    """Gold ticks stream and its minutes are written, and EUR_USD's 4h bars are read (the
    cache extends them) all through the delete and its VACUUM. Afterwards EUR_USD has no
    row anywhere (no cache row written late under its old series, no series made anew)
    and every gold minute written meanwhile is stored."""
    client = removed
    server = client.server
    token = preview(client)["token"]
    sid = call(client, server.store.series, "fake", E).id

    async def race():
        store, feed = server.store, server.feed
        stop = asyncio.Event()
        written: list[int] = []
        start = await store.last_ts("fake", G) + 60

        async def gold():
            n = 0
            while not stop.is_set():
                t = start + n * 60
                for k in range(3):
                    feed.handle_tick(Tick(G, t + k * 20 + 1, 2650.0 + k, 2650.4 + k))
                await feed.flush_m1()
                written.append(t)
                n += 1
                await asyncio.sleep(0)

        async def euro_reads():
            while not stop.is_set():
                await server.reader.candles("fake", E, TIMEFRAMES["4h"], limit=200)
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(gold()), asyncio.create_task(euro_reads())]
        await asyncio.sleep(0.05)
        result = await server.delete_instrument(E, token, confirm=E, drawings_and_alerts=False)
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.gather(*tasks)
        await feed.flush_m1()
        conn = store.conn
        left = {}
        for table in ("candles_m1", "candles_h1", "candles_cache"):
            sql = (
                f"SELECT COUNT(*) FROM {table}"
                " WHERE series=? OR series NOT IN (SELECT id FROM series)"
            )
            left[table] = (await conn.execute_fetchall(sql, (sid,)))[0][0]
        for table in ("series", "instruments", "closures", "cache_dirty"):
            sql = f"SELECT COUNT(*) FROM {table} WHERE instrument=?"
            left[table] = (await conn.execute_fetchall(sql, (E,)))[0][0]
        stamps = set(await store.timestamps("fake", G))
        # The first minute meets the builder's own gold minute; the last is still forming.
        return result, left, [t for t in written[1:-1] if t not in stamps], len(written)

    result, left, lost, n = call(client, race, timeout=60)
    assert result.rows["instruments"] == 1
    assert set(left.values()) == {0}, left
    assert n > 3 and lost == []


def test_the_delete_holds_its_locks_through_the_vacuum(removed, monkeypatch):
    """The instrument's lock (an add-back waits), the feed's rebuild lock and the cache's
    locks for that instrument are all held while the rows go and the file is compacted."""
    client = removed
    server = client.server
    seen = []
    real = purge.compact

    async def spy(conn, path):
        cache = [server.cache._locks[("fake", E, n)].locked() for n in ("4h", "1D", "1W")]
        seen.append((server.instrument_locks.busy(E), server._reconfigure_lock.locked(), cache))
        return await real(conn, path)

    monkeypatch.setattr(purge, "compact", spy)
    assert delete(client, preview(client)["token"]).status_code == 200
    assert seen == [("being deleted", True, [True, True, True])]
    assert server.instrument_locks.busy(E) is None and not server._reconfigure_lock.locked()


def test_two_deletes_at_once_delete_once(removed):
    client = removed
    server = client.server
    token = preview(client)["token"]

    async def both():
        return await asyncio.gather(
            server.delete_instrument(E, token, confirm=E, drawings_and_alerts=False),
            server.delete_instrument(E, token, confirm=E, drawings_and_alerts=False),
            return_exceptions=True,
        )

    first, second = call(client, both)
    assert not isinstance(first, BaseException)
    assert isinstance(second, purge.DeleteRefused) and second.status in (404, 409)


# The command and the server share the deletion


def test_the_command_uses_the_shared_deletion(tmp_path, monkeypatch, capsys):
    from openchart import main as cli
    from tests.test_download import seed_two

    asyncio.run(seed_two(tmp_path))
    seen = []
    shared = purge.delete_instrument

    async def spy(*args, **kwargs):
        seen.append(kwargs.get("vacuum"))
        return await shared(*args, **kwargs)

    monkeypatch.setattr(purge, "delete_instrument", spy)
    monkeypatch.setenv("OPENCHART_DATA_DIR", str(tmp_path))
    assert cli.main(["purge", "EUR_USD", "--yes"]) == 0
    assert cli.main(["purge", "XAU_USD", "--yes", "--no-vacuum"]) == 0
    assert seen == [True, False]
    out = capsys.readouterr().out
    assert "VACUUM" in out and "not compacted" in out
