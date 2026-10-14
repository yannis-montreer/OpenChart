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
