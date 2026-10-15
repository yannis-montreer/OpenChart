"""Starting the server when something is wrong, and files only their owner may read.

Found in the M8 Docker test: with a changed OPENCHART_SECRET_KEY the server logged the
error, failed its start-up, and yet the process stayed alive, holding the database open
and answering nothing, so neither Docker nor the Windows service restarted or reported
it.
"""

import asyncio
import os
import stat
import subprocess
import sys
import textwrap
import zipfile
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from openchart import main as cli
from openchart.backup import backup
from openchart.config import Config
from openchart.files import open_private
from openchart.lock import DataFolderLock
from openchart.main import EXIT_KEY_CHANGED, EXIT_STARTUP_FAILED, in_container, main
from openchart.server import Server
from openchart.settings import (
    SECRET_KEY_ENV,
    SECRET_KEY_FILE_ENV,
    EncryptedFileSecrets,
    SecretKeyChanged,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="mode bits do not apply on Windows")


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A data folder whose secrets.enc was saved with one key, and a server given
    another."""
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)
    EncryptedFileSecrets(tmp_path / "secrets.enc", Fernet.generate_key().decode()).set(
        "oanda_token", "abc"
    )
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    return tmp_path


def test_a_changed_key_stops_run_with_an_explanation(data, capsys):
    assert main(["run", "--data-dir", str(data), "--port", "0"]) == EXIT_KEY_CHANGED
    err = capsys.readouterr().err
    assert "OpenChart did not start: Cannot decrypt secrets.enc" in err
    assert "give it the key the file was saved with again" in err
    assert "enter the tokens again in Settings" in err
    assert DataFolderLock(data).acquire()  # nothing left holding the data folder


def test_a_server_whose_start_fails_closes_what_it_opened(data):
    server = Server(replace(Config(), data_dir=data), run_feed=False)
    with pytest.raises(SecretKeyChanged):
        asyncio.run(server.start())
    assert server.db is None and server.lock is None
    assert DataFolderLock(data).acquire()


def test_any_start_failure_closes_everything(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("an indicator folder that cannot be read")

    monkeypatch.setattr("openchart.server.load_indicators", broken)
    server = Server(replace(Config(), data_dir=tmp_path), run_feed=False)
    with pytest.raises(RuntimeError):
        asyncio.run(server.start())
    assert server.db is None and server.lock is None


def test_run_reports_a_failed_start_with_a_nonzero_code(tmp_path, monkeypatch):
    """So NSSM and Docker see a failure (and restart, with their back-off) instead of a
    process that looks alive."""
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)

    async def broken(self):
        raise RuntimeError("something else that stops the start")

    monkeypatch.setattr("openchart.server.Server._start", broken)
    try:  # uvicorn exits with 3 itself; older versions return and `run` returns 3
        code = main(["run", "--data-dir", str(tmp_path), "--port", "0"])
    except SystemExit as exc:
        code = exc.code
    assert code == EXIT_STARTUP_FAILED


def test_the_process_really_ends_after_a_failed_start(tmp_path):
    """The M8 bug itself: the start failed, yet the open database connection's thread
    kept the process alive. A real process, so interpreter shutdown is part of it."""
    script = textwrap.dedent(
        """
        import sys
        import openchart.server
        from openchart.main import main

        def broken():  # called once the database is open
            raise RuntimeError("fails after the database is open")

        openchart.server.load_indicators = broken
        sys.exit(main(["run", "--data-dir", sys.argv[1], "--port", "0"]))
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in (SECRET_KEY_ENV, SECRET_KEY_FILE_ENV)}
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # it hung for good before the fix
    )
    assert done.returncode == EXIT_STARTUP_FAILED, done.stderr[-2000:]
    assert "fails after the database is open" in done.stderr


def test_a_missing_secret_store_still_starts_without_tokens(tmp_path, monkeypatch):
    """No key and no credential store is not a changed key: the server starts and the
    page explains (Settings, setup), as before."""
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr("openchart.settings.KeyringSecrets.available", staticmethod(lambda: False))
    server = Server(replace(Config(), data_dir=tmp_path), run_feed=False)

    async def go():
        await server.start()
        try:
            assert server.secrets is None
            assert "No OS credential store" in server.secrets_error
        finally:
            await server.stop()

    asyncio.run(go())


# Files only their owner may read


@POSIX_ONLY
def test_secrets_enc_is_private(tmp_path):
    path = tmp_path / "secrets.enc"
    store = EncryptedFileSecrets(path, Fernet.generate_key().decode())
    store.set("oanda_token", "abc")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)  # as an earlier version wrote it
    store.set("telegram_token", "def")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get("oanda_token") == "abc"


@POSIX_ONLY
def test_backups_are_private(tmp_path):
    (tmp_path / "openchart.db").touch()
    result = backup(tmp_path)
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    with zipfile.ZipFile(result.path) as zf:
        assert "openchart.db" in zf.namelist()


def test_open_private_writes_on_every_system(tmp_path):
    with open_private(tmp_path / "x.bin") as f:
        f.write(b"\x00\x01")
    with open_private(tmp_path / "x.bin") as f:  # truncates
        f.write(b"\x02")
    assert (tmp_path / "x.bin").read_bytes() == b"\x02"


# The log line about the listening address


def test_container_detection(monkeypatch):
    monkeypatch.setattr(cli, "Path", lambda p: type("P", (), {"exists": lambda self: False})())
    monkeypatch.delenv("container", raising=False)
    assert not in_container()
    monkeypatch.setenv("container", "podman")
    assert in_container()
