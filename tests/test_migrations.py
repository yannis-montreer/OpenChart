import aiosqlite
import pytest

from openchart.store.migrate import Migration, discover_migrations, migrate
from openchart.store.sqlite import Database

SPEC_TABLES = {
    "instruments",
    "candles_m1",
    "drawings",
    "alerts",
    "alert_events",
    "layouts",
    "settings",
    "sessions",
    "candles_h1",
    "candles_cache",
}


async def _tables(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        return {row[0] for row in await cur.fetchall()}


def test_discovered_migrations_are_numbered_from_one():
    versions = [m.version for m in discover_migrations()]
    assert versions == list(range(1, len(versions) + 1))


async def test_fresh_database_has_spec_tables(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        assert await _tables(db.conn) >= SPEC_TABLES


async def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    async with await Database.open(path):
        pass
    async with aiosqlite.connect(path) as conn:
        assert await migrate(conn) == []


async def test_new_migration_applies_on_existing_database(tmp_path):
    async with aiosqlite.connect(tmp_path / "t.db") as conn:
        base = discover_migrations()
        await migrate(conn, base)
        extra = Migration(base[-1].version + 1, "extra", "CREATE TABLE extra (x INTEGER);")
        assert await migrate(conn, [*base, extra]) == [extra.version]
        assert "extra" in await _tables(conn)


async def test_failed_migration_rolls_back(tmp_path):
    async with aiosqlite.connect(tmp_path / "t.db") as conn:
        bad = Migration(1, "bad", "CREATE TABLE ok_part (x INTEGER); NOT VALID SQL;")
        with pytest.raises(aiosqlite.Error):
            await migrate(conn, [bad])
        assert "ok_part" not in await _tables(conn)
        async with conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
            assert (await cur.fetchone())[0] == 0


async def test_0006_marks_existing_history_complete_and_adds_the_rollover(tmp_path):
    """Upgrading a database from before M9's newest-first downloads."""
    from openchart.market.instruments import FX_HOURS, METAL_HOURS

    old_fx = (
        '{"tz": "America/New_York", "open": {"day": "sun", "time": "17:00"}, '
        '"close": {"day": "fri", "time": "17:00"}, "daily_breaks": []}'
    )
    async with aiosqlite.connect(tmp_path / "t.db") as conn:
        await migrate(conn, [m for m in discover_migrations() if m.version <= 5])
        for name, hours in (("EUR_USD", old_fx), ("XAU_USD", METAL_HOURS.to_json())):
            await conn.execute(
                "INSERT INTO instruments (instrument, source, provider_symbol, display_name,"
                " decimals, pip_size, enabled, hours_json) VALUES (?, 'oanda', ?, ?, 5, 1, 1, ?)",
                (name, name, name, hours),
            )
        for table, ts in (("candles_m1", 600), ("candles_h1", 0)):
            await conn.execute(
                f"INSERT INTO {table} (source, instrument, ts, volume)"
                " VALUES ('oanda', 'XAU_USD', ?, 1)",
                (ts,),
            )
        await conn.commit()
        assert await migrate(conn) == [m.version for m in discover_migrations() if m.version > 5]
        async with conn.execute(
            "SELECT instrument, h1_complete_from, m1_complete_from, hours_json FROM instruments"
            " ORDER BY instrument"
        ) as cur:
            rows = await cur.fetchall()
    assert rows == [
        ("EUR_USD", None, None, FX_HOURS.to_json()),  # no history: downloads; break added
        ("XAU_USD", 0, 600, METAL_HOURS.to_json()),  # complete; gold's hours unchanged
    ]


async def test_chart_indicators_migration_on_a_database_from_before_it(tmp_path):
    """CI-3: the charts' indicators and their revisions, beside data already stored. On a
    synthetic database only (never the owner's)."""
    import sqlite3

    (new,) = [m for m in discover_migrations() if m.name == "chart_indicators"]
    path = tmp_path / "t.db"
    state = '{"instrument": "XAU_USD", "indicators": [{"id": "ema", "params": {}}]}'
    async with aiosqlite.connect(path) as conn:
        await migrate(conn, [m for m in discover_migrations() if m.version < new.version])
        await conn.execute(
            "INSERT INTO layouts (id, name, json, updated_at) VALUES ('l1', 'Gold', ?, 5)",
            (state,),
        )
        await conn.execute(
            "INSERT INTO drawings (id, instrument, type, points_json, created_at, updated_at)"
            " VALUES ('d1', 'XAU_USD', 'hline', '[{\"t\": 1, \"p\": 2}]', 1, 1)"
        )
        await conn.commit()
        later = [m.version for m in discover_migrations() if m.version >= new.version]
        assert await migrate(conn) == later
        assert {"chart_indicators", "chart_indicator_revisions"} <= await _tables(conn)
        async with conn.execute("SELECT id, name, json, updated_at FROM layouts") as cur:
            assert await cur.fetchall() == [("l1", "Gold", state, 5)]
        async with conn.execute("SELECT COUNT(*) FROM drawings") as cur:
            assert (await cur.fetchone())[0] == 1

        row = (
            "INSERT INTO chart_indicators (source, instrument, position, uid, kind, indicator,"
            " params) VALUES (?, ?, ?, ?, ?, ?, '{}')"
        )
        await conn.execute(row, ("oanda", "XAU_USD", 0, "a", "builtin", "ema"))
        await conn.execute(row, ("oanda", "XAU_USD", 1, "b", "custom", "ema"))
        await conn.execute(row, ("oanda", "EUR_USD", 0, "a", "builtin", "ema"))  # other chart
        for bad in (
            ("oanda", "XAU_USD", 2, "c", "other", "ema"),  # kind
            ("oanda", "XAU_USD", 3, "a", "builtin", "ema"),  # uid taken on this chart
            ("oanda", "XAU_USD", 1, "d", "builtin", "ema"),  # position taken
        ):
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(row, bad)
        async with conn.execute(
            "SELECT visible, price_labels, legend_values FROM chart_indicators WHERE uid='b'"
        ) as cur:
            assert await cur.fetchone() == (1, 1, 1)
        await conn.execute(
            "INSERT INTO chart_indicator_revisions (source, instrument, revision, updated_at)"
            " VALUES ('oanda', 'XAU_USD', 1, 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO chart_indicator_revisions (source, instrument, revision, updated_at)"
                " VALUES ('oanda', 'XAU_USD', 2, 0)"
            )


async def test_indicator_alerts_migration_on_a_database_from_before_it(tmp_path):
    """0010: indicator alerts beside line alerts and their log already stored. On a
    synthetic database only (never the owner's): the old rows read back unchanged, their
    new columns are NULL, and the log takes one event per alert and bar."""
    import sqlite3

    from openchart.store.alerts import AlertStore

    (new,) = [m for m in discover_migrations() if m.name == "indicator_alerts"]
    path = tmp_path / "t.db"
    state = '{"s_prev": 1.0, "t_prev": 5, "bar_start": null}'
    async with aiosqlite.connect(path) as conn:
        await migrate(conn, [m for m in discover_migrations() if m.version < new.version])
        await conn.execute(
            "INSERT INTO alerts (id, instrument, type, direction, trigger_price, trigger_mode,"
            " timeframe, frequency, cooldown_s, enabled, state_json, created_at, name, level,"
            " notify_json, status, updated_at, fire_count) VALUES ('a1', 'XAU_USD',"
            " 'level_cross', 'up', 'bid', 'touch', '1m', 'once', 0, 1, ?, 1, 'Gold', 2000.0,"
            " '[\"app\"]', 'active', 2, 0)",
            (state,),
        )
        for k in range(2):
            await conn.execute(
                "INSERT INTO alert_events (alert_id, ts, price, message, delivered, line_value,"
                " cross, detected_at) VALUES ('a1', ?, 2001.0, 'm', 1, 2000.0, 'up', ?)",
                (10 + k, 11 + k),
            )
        await conn.commit()
        async with conn.execute("SELECT * FROM alerts") as cur:
            old_alert = await cur.fetchall()
        async with conn.execute("SELECT * FROM alert_events ORDER BY id") as cur:
            old_events = await cur.fetchall()

        later = [m.version for m in discover_migrations() if m.version >= new.version]
        assert await migrate(conn) == later
        async with conn.execute("SELECT * FROM alerts") as cur:
            (row,) = await cur.fetchall()
        assert row[: len(old_alert[0])] == old_alert[0] and row[len(old_alert[0]) :] == (None,)
        async with conn.execute("SELECT * FROM alert_events ORDER BY id") as cur:
            events = await cur.fetchall()
        assert [e[: len(old_events[0])] for e in events] == old_events
        assert all(e[len(old_events[0]) :] == (None, None, None, None) for e in events)
        (alert,) = await AlertStore(conn).list()
        assert alert.type == "level_cross" and alert.state.s_prev == 1.0
        assert alert.indicator is None and alert.stop_reason is None

        # One event per alert and bar; line events (no bar) are never in each other's way.
        insert = "INSERT INTO alert_events (alert_id, ts, bar) VALUES (?, 1, ?)"
        await conn.execute(insert, ("i1", 3600))
        await conn.execute(insert, ("i2", 3600))
        await conn.execute(insert, ("a1", None))
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(insert, ("i1", 3600))
