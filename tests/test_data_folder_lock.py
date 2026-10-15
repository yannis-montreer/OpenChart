"""The data-folder lock: commands that write refuse while a server holds the folder (and
hold it themselves while they work); commands that only read run beside a server but
never upgrade its database underneath; a second server refuses to start.

Found while applying storage step 1 to the owner's data (2026-10-15): `openchart migrate`
did not look at the lock at all, and a server only warned when another held it.
"""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace

import aiosqlite
import pytest

from openchart import main as cli
from openchart.config import Config
from openchart.lock import DataFolderInUse, DataFolderLock, exclusive, in_use
from openchart.server import Server
from openchart.settings import SECRET_KEY_ENV, SECRET_KEY_FILE_ENV
from openchart.store.migrate import discover_migrations, migrate
from openchart.store.sqlite import Database

WRITING = [
    ["migrate"],
    ["backfill", "XAU_USD"],
    ["rebuild-cache"],
    ["repair", "XAU_USD"],
    ["purge", "XAU_USD", "--yes"],
    ["reset-password"],
    ["secrets", "to-file", "--key-file", "KEYFILE"],
    ["run", "--port", "0"],
]
READING = [["stats"], ["gaps", "XAU_USD"]]


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A data folder with a current database, named by OPENCHART_DATA_DIR for every
    command, and getpass refusing to be asked (no command may prompt before refusing)."""
    folder = tmp_path / "data"
    monkeypatch.setenv("OPENCHART_DATA_DIR", str(folder))
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)

    def no_prompt(_prompt=""):
        raise AssertionError("asked for a password before checking the lock")

    monkeypatch.setattr("getpass.getpass", no_prompt)
    asyncio.run(_create(folder / "openchart.db"))
    return folder


async def _create(path, upto: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await migrate(conn, discover_migrations()[:upto] if upto else None)


def version(folder) -> int:
    uri = f"file:{(folder / 'openchart.db').as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]


def snapshot(folder) -> bytes:
    return (folder / "openchart.db").read_bytes()


@pytest.fixture
def server_running(data):
    """The folder as a running server holds it."""
    lock = DataFolderLock(data)
    assert lock.acquire()
    yield lock
    lock.release()


@pytest.mark.parametrize("argv", WRITING, ids=lambda a: " ".join(a[:2]))
def test_commands_that_write_refuse_while_a_server_runs(data, server_running, capsys, argv):
    argv = [str(data / "secret.key") if a == "KEYFILE" else a for a in argv]
    before = snapshot(data)
    assert cli.main(argv) == cli.EXIT_DATA_IN_USE
    err = capsys.readouterr().err
    assert f"OpenChart is running on {data}" in err
    assert "stop it first" in err and "Nothing was changed" in err
    assert snapshot(data) == before
    assert not (data / "secret.key").exists()


@pytest.mark.parametrize("argv", READING, ids=lambda a: a[0])
def test_commands_that_read_run_beside_a_server(data, server_running, capsys, argv):
    code = cli.main(argv)  # gaps finds no stored data here (exit 1): it ran, not refused
    out = capsys.readouterr()
    assert code != cli.EXIT_DATA_IN_USE
    assert "OpenChart is running on" not in out.err


def test_a_reading_command_never_upgrades_under_a_running_server(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "data"
    monkeypatch.setenv("OPENCHART_DATA_DIR", str(folder))
    newest = discover_migrations()[-1]
    asyncio.run(_create(folder / "openchart.db", upto=len(discover_migrations()) - 1))
    lock = DataFolderLock(folder)
    assert lock.acquire()
    try:
        assert cli.main(["stats"]) == cli.EXIT_DATA_IN_USE
        err = capsys.readouterr().err
        assert "The database needs upgrading" in err and f"{newest.version:04d}" in err
        assert "run `openchart migrate`" in err
        assert version(folder) == newest.version - 1  # untouched
    finally:
        lock.release()
    assert cli.main(["stats"]) == 0  # no server: it may upgrade
    assert version(folder) == newest.version


def test_a_second_server_refuses_to_start(data, server_running):
    server = Server(replace(Config(), data_dir=data), run_feed=False)
    with pytest.raises(DataFolderInUse, match="stop it first, then start the server again"):
        asyncio.run(server.start())
    assert server.db is None  # nothing was opened


def test_a_command_that_writes_holds_the_folder_while_it_works(data):
    with exclusive(data, "run openchart migrate"):
        assert in_use(data)
        server = Server(replace(Config(), data_dir=data), run_feed=False)
        with pytest.raises(DataFolderInUse):
            asyncio.run(server.start())
    assert not in_use(data)  # released afterwards


def test_without_a_server_the_commands_work_and_leave_the_folder_free(data):
    assert cli.main(["migrate"]) == 0
    assert cli.main(["stats"]) == 0
    assert not in_use(data)


async def test_opening_without_upgrade_reports_what_is_pending(tmp_path):
    path = tmp_path / "old.db"
    await _create(path, upto=2)
    from openchart.store.migrate import UpgradeNeeded

    with pytest.raises(UpgradeNeeded, match="0003"):
        await Database.open(path, upgrade=False)
    async with await Database.open(path) as db:  # with upgrade (the default) it runs
        rows = await db.conn.execute_fetchall("SELECT MAX(version) FROM schema_migrations")
    assert rows[0][0] == discover_migrations()[-1].version
