"""Who may edit custom indicator code (the D2 gate), and `openchart settings`, the one
command that writes beside a running server (owner's decision, CI-1)."""

import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from openchart.api.editing import FORWARDING_HEADERS, decide, is_loopback
from openchart.indicators.worker.isolation import Isolation
from openchart.main import main
from openchart.settings import new_secret_key
from openchart.store.sqlite import Database

ISOLATED = Isolation("appcontainer", True, "minimum isolation met")
WEAK = Isolation("restricted", False, "restricted: below the minimum isolation")


def gate(setting="auto", *, peer="127.0.0.1", headers=None, behind_proxy=False, isolation=ISOLATED):
    return decide(
        setting=setting,
        behind_proxy=behind_proxy,
        isolation=isolation,
        peer=peer,
        headers=headers or {},
    )


# The gate --------------------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.8.9.10", "::1"])
def test_auto_allows_the_servers_own_machine(peer):
    assert gate(peer=peer).allowed


@pytest.mark.parametrize(
    "peer", ["100.101.102.103", "192.168.1.20", "172.17.0.1", "", None, "testclient"]
)
def test_auto_refuses_other_devices_tailscale_and_the_docker_bridge(peer):
    editing = gate(peer=peer)
    assert not editing.allowed and "own machine" in editing.reason


@pytest.mark.parametrize("header", FORWARDING_HEADERS)
def test_auto_refuses_a_request_that_came_through_a_proxy(header):
    editing = gate(headers={header: "203.0.113.9"})
    assert not editing.allowed and header in editing.reason


def test_auto_refuses_behind_a_proxy():
    assert not gate(behind_proxy=True).allowed


def test_on_allows_from_anywhere_but_never_below_the_minimum():
    assert gate("on", peer="100.101.102.103", headers={"x-forwarded-for": "x"}).allowed
    refused = gate("on", isolation=WEAK)
    assert not refused.allowed and "does not isolate" in refused.reason
    assert not gate("on", isolation=Isolation("unavailable", False, "D7 a")).allowed
    assert not gate("on", isolation=None).allowed  # the worker is still starting


def test_off_refuses():
    editing = gate("off")
    assert not editing.allowed and "openchart settings set" in editing.reason


def test_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("fe80::1%3") is False
    assert not is_loopback("localhost")  # a peer is an address, never a name


# The catalog shows it --------------------------------------------------------------------


def test_the_catalog_reads_the_setting_on_every_request(client, harness):
    def editing():
        return client.get("/api/indicators").json()["editing"]

    assert editing()["setting"] == "auto"
    db = harness.config.db_path
    assert main(["settings", "set", "indicator-editing", "off", "--data-dir", str(db.parent)]) == 0
    assert editing()["setting"] == "off"  # no restart
    assert main(["settings", "set", "indicator-editing", "on", "--data-dir", str(db.parent)]) == 0
    assert editing()["setting"] == "on"
    assert editing()["allowed"] is False  # the test server runs no worker: below the minimum


def test_no_http_endpoint_changes_the_setting(client, harness):
    body = {"indicator_editing": "on", "behind_proxy": True}
    client.put("/api/settings", json=body)
    for path in ("/api/settings", "/api/indicators", "/api/indicators/settings"):
        for method in ("post", "put", "patch"):
            getattr(client, method)(path, json=body)
    assert client.get("/api/indicators").json()["editing"]["setting"] == "auto"
    with sqlite3.connect(harness.config.db_path) as conn:
        keys = {r[0] for r in conn.execute("SELECT key FROM settings")}
    assert not keys & {"indicator_editing", "behind_proxy"}
    routes = " ".join(getattr(r, "path", "") for r in client.app.routes)
    assert "editing" not in routes and "proxy" not in routes


# `openchart settings` ---------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    async def make():
        async with await Database.open(tmp_path / "openchart.db"):
            pass

    asyncio.run(make())
    return tmp_path


def stored(data_dir: Path) -> dict:
    with sqlite3.connect(data_dir / "openchart.db") as conn:
        return dict(conn.execute("SELECT key, value FROM settings"))


def test_set_and_get(data_dir, capsys):
    assert main(["settings", "get", "--data-dir", str(data_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "indicator-editing: auto (default)",
        "behind-proxy: off (default)",
    ]
    assert main(["settings", "set", "indicator-editing", "ON", "--data-dir", str(data_dir)]) == 0
    assert main(["settings", "set", "behind-proxy", "yes", "--data-dir", str(data_dir)]) == 0
    assert stored(data_dir) == {"indicator_editing": '"on"', "behind_proxy": "true"}
    capsys.readouterr()
    assert main(["settings", "get", "behind-proxy", "--data-dir", str(data_dir)]) == 0
    assert capsys.readouterr().out.strip() == "behind-proxy: on"


@pytest.mark.parametrize(
    "argv",
    [
        ["set", "indicator-editing", "maybe"],
        ["set", "behind-proxy", "2"],
        ["set", "listen_on_network", "true"],
        ["set", "instance_id", "x"],
        ["set", "oanda_token", "x"],
        ["set", "indicator-editing"],
    ],
)
def test_only_its_two_keys_with_valid_values(data_dir, argv):
    before = stored(data_dir)
    assert main(["settings", *argv, "--data-dir", str(data_dir)]) == 2
    assert stored(data_dir) == before


def test_a_pending_migration_refuses(data_dir, capsys):
    with sqlite3.connect(data_dir / "openchart.db") as conn:
        last = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (last,))
    assert main(["settings", "set", "indicator-editing", "on", "--data-dir", str(data_dir)]) == 1
    assert "needs upgrading" in capsys.readouterr().err
    assert "indicator_editing" not in stored(data_dir)


def test_a_locked_database_fails_quickly_and_changes_nothing(data_dir, capsys):
    holder = sqlite3.connect(data_dir / "openchart.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the write lock
    try:
        t = time.perf_counter()
        assert (
            main(["settings", "set", "indicator-editing", "on", "--data-dir", str(data_dir)]) == 1
        )
        assert time.perf_counter() - t < 6
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "busy" in capsys.readouterr().err
    assert "indicator_editing" not in stored(data_dir)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_it_runs_beside_a_real_server_process_without_its_lock(data_dir):
    """A real `openchart run` holds the folder (no broker credentials: no network); the
    command writes anyway, `migrate` still refuses, and the server keeps running."""
    port = free_port()
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("OPENCHART_", "OANDA"))},
        "PYTHONPATH": src,
        "OPENCHART_SECRET_KEY": new_secret_key(),  # an encrypted file, never the keyring
        "OPENCHART_LOG_LEVEL": "WARNING",
    }
    cmd = [sys.executable, "-m", "openchart.main"]
    server = subprocess.Popen(
        [*cmd, "run", "--data-dir", str(data_dir), "--port", str(port), "--host", "127.0.0.1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        end = time.monotonic() + 60
        while True:
            assert server.poll() is None, "the server ended"
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < end, "the server did not start"
            time.sleep(0.2)

        def run(*args):
            return subprocess.run(
                [*cmd, *args],
                env={**env, "OPENCHART_DATA_DIR": str(data_dir)},
                capture_output=True,
                text=True,
                timeout=60,
            )

        done = run("settings", "set", "indicator-editing", "on")
        assert done.returncode == 0, done.stderr
        assert "is now on" in done.stdout
        assert stored(data_dir)["indicator_editing"] == '"on"'
        assert run("migrate").returncode == 4  # the server still holds the folder
        assert run("settings", "get").stdout.splitlines()[0] == "indicator-editing: on"
        assert httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=5).status_code == 200
    finally:
        server.terminate()
        server.wait(30)
