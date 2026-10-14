"""`openchart backup` (a consistent copy while the server writes) and moving the tokens to
the encrypted file for the Windows service."""

import json
import sqlite3
import threading
import time
import zipfile
from datetime import datetime

import pytest

from openchart.backup import backup
from openchart.main import main
from openchart.settings import (
    SECRET_KEY_ENV,
    SECRET_KEY_FILE_ENV,
    EncryptedFileSecrets,
    MemorySecrets,
    SecretStoreUnavailable,
    new_secret_key,
    open_secrets,
    secret_key_from_env,
    secrets_to_file,
)
from tests.conftest import Harness


def make_data(folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "openchart.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE rows (n INTEGER PRIMARY KEY, a INTEGER, b INTEGER)")
    db.execute("INSERT INTO rows VALUES (0, 0, 0)")
    db.commit()
    db.close()
    ticks = folder / "ticks" / "oanda" / "XAU_USD" / "date=2026-10-13"
    ticks.mkdir(parents=True)
    (ticks / "data.parquet").write_bytes(b"PAR1 fake parquet")
    (folder / "indicators").mkdir()
    (folder / "indicators" / "mine.py").write_text("NAME = 'x'\n")
    (folder / "secrets.enc").write_bytes(b"gAAAA-encrypted")
    (folder / "logs").mkdir()
    (folder / "logs" / "service.log").write_text("log\n")
    (folder / "openchart.db.tmp").write_text("leftover")


def test_a_backup_while_the_database_is_written_is_consistent(tmp_path):
    data = tmp_path / "data"
    make_data(data)
    stop = threading.Event()

    def writer():
        # Every transaction keeps a == b; a torn copy would break it.
        db = sqlite3.connect(data / "openchart.db", timeout=30)
        n = 1
        while not stop.is_set():
            db.execute("INSERT INTO rows VALUES (?, ?, ?)", (n, n, n))
            db.execute("UPDATE rows SET a = a + 1, b = b + 1 WHERE n = 0")
            db.commit()
            n += 1
        db.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.2)
        result = backup(data, now=1_791_900_000)
    finally:
        stop.set()
        thread.join()

    local = datetime.fromtimestamp(1_791_900_000).strftime("%Y%m%d-%H%M%S")
    assert result.path == data / "backups" / f"openchart-backup-{local}.zip"
    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
        assert names == {
            "openchart.db",
            "ticks/oanda/XAU_USD/date=2026-10-13/data.parquet",
            "indicators/mine.py",
            "secrets.enc",
            "backup.json",
        }  # no logs, no leftovers, no -wal/-shm
        assert zf.getinfo("ticks/oanda/XAU_USD/date=2026-10-13/data.parquet").compress_type == 0
        assert json.loads(zf.read("backup.json"))["files"] == 4
        zf.extract("openchart.db", tmp_path / "restored")
    copy = sqlite3.connect(tmp_path / "restored" / "openchart.db")
    a, b = copy.execute("SELECT a, b FROM rows WHERE n = 0").fetchone()
    count = copy.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    assert a == b == count - 1 > 0  # one snapshot: the counter matches the rows
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert result.has_secrets and result.tick_files == 1
    # The next backup does not include the previous one.
    second = backup(data, now=1_791_900_060)
    with zipfile.ZipFile(second.path) as zf:
        assert not any(n.startswith("backups/") for n in zf.namelist())


def test_backup_options_and_errors(tmp_path, capsys):
    data = tmp_path / "data"
    make_data(data)
    out = tmp_path / "elsewhere"
    assert main(["backup", "--data-dir", str(data), "--out", str(out), "--no-ticks"]) == 0
    printed = capsys.readouterr().out
    [zip_path] = out.glob("openchart-backup-*.zip")
    assert f"Backup written: {zip_path}" in printed
    assert "secrets.enc is in it, encrypted; its key is not" in printed
    with zipfile.ZipFile(zip_path) as zf:
        assert not any(n.startswith("ticks/") for n in zf.namelist())
    assert main(["backup", "--data-dir", str(tmp_path / "empty")]) == 1
    assert "No database" in capsys.readouterr().err


def test_stats_counts_what_a_move_must_keep(client, capsys):
    """`openchart stats` before and after moving a data folder: the same counts."""
    hline = {"instrument": "XAU_USD", "type": "hline", "points": [{"t": 1, "p": 2000.0}]}
    line = client.post("/api/drawings", json=hline).json()
    body = {"instrument": "XAU_USD", "type": "line_cross", "drawing_id": line["id"]}
    client.post("/api/alerts", json=body)
    stopped = client.post("/api/alerts", json=body).json()
    client.put(f"/api/alerts/{stopped['id']}", json={"status": "stopped"})
    client.post("/api/layouts", json={"name": "Gold", "layout": {}})
    data = str(client.harness.config.data_dir)
    assert main(["stats", "--data-dir", data]) == 0
    printed = capsys.readouterr().out
    assert "fake/XAU_USD:" in printed
    assert "Drawings: 1, alerts: 2 (1 active), layouts: 1" in printed


# Secrets for the Windows service


def test_the_key_can_come_from_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)
    assert secret_key_from_env() is None
    key = new_secret_key()
    key_file = tmp_path / "secret.key"
    key_file.write_text(key + "\n")
    monkeypatch.setenv(SECRET_KEY_FILE_ENV, str(key_file))
    assert secret_key_from_env() == key
    store = open_secrets(Harness(tmp_path).config, "instance")
    assert isinstance(store, EncryptedFileSecrets)
    store.set("oanda_token", "abc")
    assert EncryptedFileSecrets(tmp_path / "secrets.enc", key).get("oanda_token") == "abc"

    monkeypatch.setenv(SECRET_KEY_FILE_ENV, str(tmp_path / "missing.key"))
    with pytest.raises(SecretStoreUnavailable, match="Cannot read the secret key file"):
        secret_key_from_env()
    monkeypatch.setenv(SECRET_KEY_ENV, key)  # the variable itself wins
    assert secret_key_from_env() == key


def test_tokens_move_from_the_credential_store_to_the_file(tmp_path):
    source = MemorySecrets()
    source.set("oanda_token", "oanda-SECRET")
    key_file = tmp_path / "ProgramData" / "secret.key"
    moved = secrets_to_file(tmp_path / "data", key_file, source)
    assert moved.key_created and moved.copied == ["oanda_token"]
    assert moved.missing == ["telegram_token"]
    key = key_file.read_text().strip()
    stored = EncryptedFileSecrets(tmp_path / "data" / "secrets.enc", key)
    assert stored.get("oanda_token") == "oanda-SECRET"
    assert b"SECRET" not in (tmp_path / "data" / "secrets.enc").read_bytes()

    # Run again (reinstall): the same key, nothing lost, a later token copied too.
    stored.set("telegram_token", "set-in-the-web-page")
    again = secrets_to_file(tmp_path / "data", key_file, MemorySecrets())
    assert not again.key_created and again.copied == [] and again.missing == []
    assert stored.get("telegram_token") == "set-in-the-web-page"

    # A key that does not open the existing file is refused, not overwritten.
    other = tmp_path / "other.key"
    other.write_text(new_secret_key())
    with pytest.raises(SecretStoreUnavailable):
        secrets_to_file(tmp_path / "data", other, source)
    assert stored.get("oanda_token") == "oanda-SECRET"


def test_secrets_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_ENV, raising=False)
    assert main(["secrets", "new-key"]) == 0
    assert len(capsys.readouterr().out.strip()) == 44
    harness = Harness(tmp_path)
    harness.seed_candles()  # creates the database
    monkeypatch.setattr("openchart.settings.KeyringSecrets.available", staticmethod(lambda: False))
    key_file = tmp_path / "keys" / "secret.key"
    assert (
        main(["secrets", "to-file", "--data-dir", str(tmp_path), "--key-file", str(key_file)]) == 0
    )
    out = capsys.readouterr().out
    assert "(created)" in out and "Copied from the Credential Manager: nothing" in out
    monkeypatch.setenv(SECRET_KEY_FILE_ENV, str(key_file))
    assert main(["secrets", "status", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "encrypted file" in out and "oanda_token: not set" in out
    with pytest.raises(SystemExit):
        main(["secrets", "to-file"])  # needs --key-file
