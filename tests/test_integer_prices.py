"""Storage step 1 (docs/storage-review.md, layout A+C): candle prices as scaled integers
under a small series id. The migration from the float layout must give back every
stored price bit for bit, or change nothing."""

import logging
import sqlite3
from contextlib import closing

import aiosqlite
import pytest

from openchart.providers.base import OHLC, Candle
from openchart.store.candles import DEFAULT_SCALE, CandleStore, price_scale
from openchart.store.migrate import discover_migrations, migrate
from openchart.store.sqlite import Database

OLD = [m for m in discover_migrations() if m.version <= 6]
PRICES = ", ".join(f"{s}_{x}" for s in ("bid", "ask", "mid") for x in "ohlc")


def candle(ts: int, bid: float, ask: float, mid: float, step: float, digits: int) -> Candle:
    """bid/ask with `digits` decimals, mid with one more (OANDA's half-pips)."""

    def ohlc(p: float, nd: int) -> OHLC:
        return OHLC(round(p, nd), round(p + 2 * step, nd), round(p - step, nd), round(p + step, nd))

    return Candle(ts, ohlc(bid, digits), ohlc(ask, digits), ohlc(mid, digits + 1), volume=ts % 97)


# Prices that are awkward in binary: thirds, values just under round numbers, many digits.
GOLD = [
    candle(1_700_000_000 + 60 * i, 2650.123 + i / 3, 2650.456 + i / 3, 2650.2895 + i / 3, 0.001, 3)
    for i in range(500)
]
EURO = [
    candle(
        1_700_000_000 + 60 * i, 1.08123 + i / 7e5, 1.08135 + i / 7e5, 1.081295 + i / 7e5, 0.00001, 5
    )
    for i in range(500)
]
NO_META = [
    candle(1_700_000_000 + 60 * i, 99.987654 - i / 1e6, 99.99, 99.9888, 0.000001, 6)
    for i in range(50)
]


async def old_database(path) -> None:
    """A database as M9 left it: float prices, rows keyed by source and instrument."""
    async with aiosqlite.connect(path) as conn:
        await migrate(conn, OLD)
        for name, decimals in (("XAU_USD", 3), ("EUR_USD", 5)):
            await conn.execute(
                "INSERT INTO instruments (instrument, source, provider_symbol, display_name,"
                " decimals, pip_size, enabled) VALUES (?, 'oanda', ?, ?, ?, 0.01, 1)",
                (name, name, name, decimals),
            )
        rows = [
            ("oanda", name, c.ts, *c.bid, *c.ask, *c.mid, c.volume)
            for name, candles in (("XAU_USD", GOLD), ("EUR_USD", EURO), ("TOOL_X", NO_META))
            for c in candles
        ]
        cols = f"source, instrument, ts, {PRICES}, volume"
        marks = ", ".join("?" * 16)
        for table in ("candles_m1", "candles_h1"):
            await conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", rows)
        await conn.executemany(
            f"INSERT INTO candles_cache (timeframe, {cols}) VALUES ('1D', {marks})", rows
        )
        await conn.commit()


def all_old_rows(path) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        out = {}
        for table in ("candles_m1", "candles_h1", "candles_cache"):
            out[table] = conn.execute(
                f"SELECT source, instrument, ts, {PRICES}, volume FROM {table} ORDER BY 1, 2, 3"
            ).fetchall()
        return out


async def test_the_migration_gives_back_every_price_bit_for_bit(tmp_path):
    path = tmp_path / "t.db"
    await old_database(path)
    before = all_old_rows(path)
    size_before = path.stat().st_size

    async with await Database.open(path) as db:  # applies 0007, then VACUUM
        store = CandleStore(db.conn)
        series = {
            r[0]: r[1:]
            for r in await db.conn.execute_fetchall(
                "SELECT instrument, id, scale FROM series ORDER BY id"
            )
        }
        assert {k: v[1] for k, v in series.items()} == {
            "EUR_USD": 10**6,  # 5 decimals + 1
            "TOOL_X": DEFAULT_SCALE,  # no metadata
            "XAU_USD": 10**4,  # 3 decimals + 1
        }
        for table, gran in (("candles_m1", "M1"), ("candles_h1", "H1")):
            got = [
                ("oanda", name, c.ts, *c.bid, *c.ask, *c.mid, c.volume)
                for name in sorted(series)
                for c in await store.read("oanda", name, 0, 2**40, gran)
            ]
            assert got == before[table]  # == on floats: bit for bit
            raw = await db.conn.execute_fetchall(
                f"SELECT typeof(bid_o), typeof(mid_h) FROM {table}"
            )
            assert set(raw) == {("integer", "integer")}
        rebuilt = []
        for name in sorted(series):
            b, a, m = [
                await store.read_cache("oanda", name, "1D", 0, 2**40, price)
                for price in ("bid", "ask", "mid")
            ]
            for i in range(len(b)):
                prices = [*b.iloc[i, 1:5], *a.iloc[i, 1:5], *m.iloc[i, 1:5]]
                rebuilt.append(("oanda", name, int(b.ts[i]), *prices, int(b.v[i])))
        assert rebuilt == before["candles_cache"]
        version = await db.conn.execute_fetchall("SELECT MAX(version) FROM schema_migrations")
        assert version[0][0] >= 7
        free = await db.conn.execute_fetchall("PRAGMA freelist_count")
        assert free[0][0] == 0  # vacuumed: the old tables' pages are given back
    assert path.stat().st_size < size_before


async def test_a_price_that_does_not_convert_exactly_stops_the_migration(tmp_path):
    path = tmp_path / "t.db"
    await old_database(path)
    with closing(sqlite3.connect(path)) as conn:  # gold with more decimals than its scale holds
        conn.execute(
            "UPDATE candles_m1 SET bid_c = 2650.123456 WHERE instrument='XAU_USD' AND ts=1700000060"
        )
        conn.commit()
    before = all_old_rows(path)

    with pytest.raises(Exception, match="did not convert exactly"):
        async with await Database.open(path):
            pass
    assert all_old_rows(path) == before  # nothing changed
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='series'").fetchone() is None


async def test_writes_round_trip_and_extra_digits_are_logged(tmp_path, caplog):
    async with await Database.open(tmp_path / "t.db") as db:
        store = CandleStore(db.conn)
        assert price_scale(3) == 10**4 and price_scale(None) == DEFAULT_SCALE
        await store.upsert("fake", "XAU_USD", GOLD)
        await store.upsert("fake", "EUR_USD", EURO, "H1")
        assert await store.read("fake", "XAU_USD", 0, 2**40) == GOLD
        assert await store.read("fake", "EUR_USD", 0, 2**40, "H1") == EURO

        # A series without metadata keeps six decimals; a seventh is rounded and logged.
        odd = Candle(1, OHLC(1.1234567, 2, 1, 1.5), OHLC(2, 2, 2, 2), OHLC(2, 2, 2, 2), 1)
        with caplog.at_level(logging.WARNING):
            await store.upsert("fake", "ODD", [odd])
        [back] = await store.read("fake", "ODD", 0, 10)
        assert back.bid.o == 1.123457
        assert "more decimals than the series stores" in caplog.text

        # Purging an instrument removes its series; it starts afresh when added again.
        counts = await store.purge_instrument("fake", "XAU_USD")
        assert counts["series"] == 1 and counts["candles_m1"] == len(GOLD)
        assert await store.series("fake", "XAU_USD") is None
        assert await store.read("fake", "XAU_USD", 0, 2**40) == []


# The migration on real installs: identity, backups, the command line


async def test_the_database_identity_and_the_tokens_survive_the_migration(tmp_path, monkeypatch):
    """Credential Manager entries are found by the database's instance_id (service
    `OpenChart/<instance_id>`), and the service's secrets.enc sits next to the database;
    both must still work after the migration and its VACUUM."""
    from openchart.settings import (
        SECRET_KEY_ENV,
        SECRET_KEY_FILE_ENV,
        EncryptedFileSecrets,
        KeyringSecrets,
        SettingsStore,
        new_secret_key,
        open_secrets,
    )
    from tests.conftest import Harness

    path = tmp_path / "openchart.db"
    await old_database(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('instance_id', '\"5eed1234abcd5678\"')"
        )
        conn.commit()
    key = new_secret_key()
    EncryptedFileSecrets(tmp_path / "secrets.enc", key).set("oanda_token", "token-SECRET")

    async with await Database.open(path) as db:
        instance = await SettingsStore(db.conn).instance_id()
    assert instance == "5eed1234abcd5678"
    assert KeyringSecrets(instance).service == "OpenChart/5eed1234abcd5678"
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(SECRET_KEY_ENV, key)
    store = open_secrets(Harness(tmp_path).config, instance)
    assert isinstance(store, EncryptedFileSecrets)
    assert store.get("oanda_token") == "token-SECRET"


def test_a_backup_from_before_the_migration_restores_and_migrates(tmp_path, capsys):
    import asyncio

    from openchart.backup import backup
    from openchart.main import main

    data = tmp_path / "data"
    data.mkdir()
    asyncio.run(old_database(data / "openchart.db"))
    before = all_old_rows(data / "openchart.db")
    old_zip = backup(data, tmp_path / "zips").path  # taken before the upgrade

    # Restored as the README says (unzip into an empty folder), then opened by a new
    # server: the migration runs on the restored database.
    import zipfile

    restored = tmp_path / "restored"
    with zipfile.ZipFile(old_zip) as zf:
        zf.extractall(restored)

    async def read_back():
        async with await Database.open(restored / "openchart.db") as db:
            store = CandleStore(db.conn)
            return [
                ("oanda", name, c.ts, *c.bid, *c.ask, *c.mid, c.volume)
                for name in ("EUR_USD", "TOOL_X", "XAU_USD")
                for c in await store.read("oanda", name, 0, 2**40)
            ]

    got = asyncio.run(read_back())
    assert got == before["candles_m1"]

    # The command line on the migrated database, pointed at it with --data-dir.
    assert main(["stats", "--data-dir", str(restored)]) == 0
    out = capsys.readouterr().out
    assert "oanda/XAU_USD: 500 M1 candles" in out and "oanda/EUR_USD: 500 M1 candles" in out
    assert main(["backup", "--data-dir", str(restored), "--out", str(tmp_path / "zips2")]) == 0
    assert "Backup written" in capsys.readouterr().out
    assert main(["purge", "EUR_USD", "--yes", "--data-dir", str(restored)]) == 0
    capsys.readouterr()
    assert main(["stats", "--data-dir", str(restored)]) == 0
    out = capsys.readouterr().out
    assert "EUR_USD" not in out and "oanda/XAU_USD: 500 M1 candles" in out
    with closing(sqlite3.connect(restored / "openchart.db")) as conn:
        assert conn.execute("SELECT instrument FROM series ORDER BY 1").fetchall() == [
            ("TOOL_X",),
            ("XAU_USD",),
        ]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


async def test_the_migration_checks_free_disk_space_first(tmp_path, monkeypatch):
    """The rewrite and VACUUM need room (1.4 times the database on the owner's data):
    without 1.5 times its size free, nothing starts and the message says why."""
    from collections import namedtuple

    from openchart.store import migrate as migrate_module

    path = tmp_path / "t.db"
    await old_database(path)
    before = all_old_rows(path)
    size = path.stat().st_size
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        migrate_module.shutil, "disk_usage", lambda p: usage(10**12, 0, int(size * 1.2))
    )
    with pytest.raises(migrate_module.MigrationError) as raised:
        async with await Database.open(path):
            pass
    message = str(raised.value)
    assert "Not enough free disk space" in message and "Nothing was changed" in message
    assert f"{size * 1.5 / 1e6:,.0f} MB free" in message
    assert all_old_rows(path) == before
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6

    monkeypatch.undo()  # the real free space again
    async with await Database.open(path) as db:  # with room, it runs
        rows = await db.conn.execute_fetchall("SELECT MAX(version) FROM schema_migrations")
        assert rows[0][0] >= 7
