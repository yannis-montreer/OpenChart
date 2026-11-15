"""The Telegram "alert engine started" message is sent when it carries information.

`telegram-start-message`: `auto` (the default) sends it after a run that did not stop
cleanly, or when the engine was down for more than `START_GAP_SECONDS`; `always` sends it
at every start (as before); `never` never does. Everything runs on a fake clock and a fake
Telegram (an httpx mock transport that records the texts).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.request

import httpx
import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.main import main
from openchart.providers.base import Tick
from openchart.server import Server
from tests.bounded import call
from tests.conftest import PASSWORD, Harness, run_setup

TELEGRAM_TOKEN = "123456:telegram-SECRET-token"
T0 = 1_791_990_000.0
STARTED = "▶️ OpenChart alert engine started"
OLD_TEXT = (  # the message as it has always been, with no alerts
    "▶️ OpenChart alert engine started: watching 0 active alerts (no instrument)."
    " The watchdog reports a stalled price feed."
)


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Telegram:
    """A fake Telegram: records the text of every message sent."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    def started(self) -> list[str]:
        return [t for t in self.texts if t.startswith(STARTED)]


def set_mode(harness: Harness, mode: str) -> None:
    with sqlite3.connect(harness.config.db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('telegram_start_message', ?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (json.dumps(mode),),
        )


def a_run(harness, clock, tg, *, first=False, at=None, stay=None, kill=False, monkeypatch=None):
    """One server run: start at `at` (clock seconds), stop `stay` seconds later.
    `kill`: the process dies before it can note a clean stop."""
    if at is not None:
        clock.t = at
    server = (
        harness.server
        if first
        else Server(harness.config, provider=harness.provider, secrets=harness.secrets)
    )
    server.now = clock
    server.telegram_transport = tg.transport
    if first:
        harness.secrets.set("telegram_token", TELEGRAM_TOKEN)
    if kill:
        monkeypatch.setattr(Server, "_record_clean_stop", lambda self: None, raising=False)
    with TestClient(create_app(server)) as c:
        c.server = server
        if first:
            assert run_setup(c, telegram_chat_id="42").status_code == 200
        call(c, server.alerts.dispatcher.drain)
        if stay:
            clock.t += stay
    return server


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_a_clean_stop_then_a_manual_start_sends_nothing(h):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    a_run(h, clock, tg, at=clock.t + 60, stay=30)
    a_run(h, clock, tg, at=clock.t + 90)
    assert tg.started() == []


def test_a_first_start_sends_nothing(h):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True)
    assert tg.started() == []


def test_a_run_killed_without_the_marker_sends_one_message_saying_so(h, monkeypatch):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    a_run(h, clock, tg, at=clock.t + 10, stay=10, kill=True, monkeypatch=monkeypatch)
    monkeypatch.undo()
    a_run(h, clock, tg, at=clock.t + 20)
    [text] = tg.started()
    assert "did not stop cleanly" in text
    assert "watching 0 active alerts" in text  # still says what it watches


def test_the_marker_is_cleared_at_start_so_it_reports_once(h, monkeypatch):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    a_run(h, clock, tg, at=clock.t + 10, kill=True, monkeypatch=monkeypatch)
    monkeypatch.undo()
    a_run(h, clock, tg, at=clock.t + 10, stay=10)  # reports the kill, then stops cleanly
    a_run(h, clock, tg, at=clock.t + 10)
    assert len(tg.started()) == 1


def test_a_long_gap_sends_one_message_with_its_length(h):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    a_run(h, clock, tg, at=clock.t + 20 * 60)
    [text] = tg.started()
    assert "was down for 20 min" in text
    assert "did not stop cleanly" not in text


def test_the_gap_threshold_is_more_than_five_minutes(h):
    from openchart.alerts.service import START_GAP_SECONDS

    assert START_GAP_SECONDS == 300
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=1)
    a_run(h, clock, tg, at=clock.t + START_GAP_SECONDS, stay=1)  # exactly 5 min: nothing
    assert tg.started() == []
    a_run(h, clock, tg, at=clock.t + START_GAP_SECONDS + 1)  # a second more: one
    assert len(tg.started()) == 1


def test_never_sends_nothing_even_after_a_kill_or_a_gap(h, monkeypatch):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    set_mode(h, "never")
    a_run(h, clock, tg, at=clock.t + 3600, kill=True, monkeypatch=monkeypatch)
    monkeypatch.undo()
    a_run(h, clock, tg, at=clock.t + 3600, stay=10)
    a_run(h, clock, tg, at=clock.t + 10)
    assert tg.started() == []


def test_always_sends_it_as_before_at_every_start(h):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    set_mode(h, "always")
    a_run(h, clock, tg, at=clock.t + 5, stay=5)
    a_run(h, clock, tg, at=clock.t + 5)
    assert tg.started() == [OLD_TEXT, OLD_TEXT]


def test_a_data_folder_from_before_this_change_is_not_called_a_crash(h):
    """No run-state file yet (an install from before): nothing is known, so say nothing."""
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    for f in h.config.data_dir.glob("run-state*"):
        f.unlink()
    a_run(h, clock, tg, at=clock.t + 10)
    assert tg.started() == []


# The setting -------------------------------------------------------------------------------


def test_the_setting_in_the_settings_page(client):
    first = client.get("/api/settings").json()
    assert first["telegram"]["start_message"] == "auto"
    assert first["telegram"]["start_message_choices"] == ["auto", "always", "never"]
    out = client.put("/api/settings", json={"telegram_start_message": "never"})
    assert out.status_code == 200 and out.json()["telegram"]["start_message"] == "never"
    assert client.get("/api/settings").json()["telegram"]["start_message"] == "never"
    bad = client.put("/api/settings", json={"telegram_start_message": "sometimes"})
    assert bad.status_code == 422
    assert client.get("/api/settings").json()["telegram"]["start_message"] == "never"


def test_the_setting_needs_a_login_and_the_same_origin(client):
    evil = {"Origin": "https://evil.example"}
    body = {"telegram_start_message": "always"}
    assert client.put("/api/settings", json=body, headers=evil).status_code == 403
    client.cookies.clear()
    assert client.put("/api/settings", json=body).status_code == 401
    client.post("/api/login", json={"password": PASSWORD})
    assert client.get("/api/settings").json()["telegram"]["start_message"] == "auto"


def test_the_settings_command(h, capsys):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True)
    d = str(h.config.data_dir)
    assert main(["settings", "get", "telegram-start-message", "--data-dir", d]) == 0
    assert capsys.readouterr().out.strip() == "telegram-start-message: auto (default)"
    assert main(["settings", "set", "telegram-start-message", "Never", "--data-dir", d]) == 0
    capsys.readouterr()
    assert main(["settings", "get", "telegram-start-message", "--data-dir", d]) == 0
    assert capsys.readouterr().out.strip() == "telegram-start-message: never"
    assert main(["settings", "set", "telegram-start-message", "often", "--data-dir", d]) == 2
    assert main(["settings", "get", "telegram-start-message", "--data-dir", d]) == 0
    assert capsys.readouterr().out.strip().endswith("never")  # unchanged by the bad value


def test_a_value_set_in_the_command_is_what_the_server_uses(h, monkeypatch):
    clock, tg = Clock(), Telegram()
    a_run(h, clock, tg, first=True, stay=30)
    d = str(h.config.data_dir)
    assert main(["settings", "set", "telegram-start-message", "always", "--data-dir", d]) == 0
    a_run(h, clock, tg, at=clock.t + 5)
    assert tg.started() == [OLD_TEXT]


# What does not change ----------------------------------------------------------------------


def line_alert_run(tmp_path, mode: str):
    """Set up, set the mode, create a level alert, cross it twice on live ticks. Returns
    what Telegram got other than start messages, and the stored events (without ids)."""
    h = Harness(tmp_path / mode)
    h.seed_candles()
    tg = Telegram()
    h.server.telegram_transport = tg.transport
    h.secrets.set("telegram_token", TELEGRAM_TOKEN)
    with TestClient(create_app(h.server)) as c:
        c.server = h.server
        assert run_setup(c, telegram_chat_id="42").status_code == 200
        assert c.put("/api/settings", json={"telegram_start_message": mode}).status_code == 200
        body = {
            "instrument": "XAU_USD",
            "type": "level_cross",
            "level": 2000.0,
            "frequency": "repeating",
        }
        assert c.post("/api/alerts", json=body).status_code == 200
        t = time.time()
        for i, price in enumerate((1990.0, 2010.0, 1990.0)):
            call(
                c, h.server.feed.handle_tick, Tick("XAU_USD", t + 20 * i, price - 0.1, price + 0.1)
            )
        for _ in range(3):
            call(c, h.server.alerts.dispatcher.drain)
            time.sleep(0.05)
        events = [
            {
                k: (v.rsplit("\n", 1)[0] if k == "message" else v)
                for k, v in e.items()
                if k not in ("id", "ts", "detected_at", "alert_id")
            }
            for e in c.get("/api/alerts/events").json()
        ]
    # Without the last line of each text: the time of the tick, which differs per run.
    others = [t.rsplit("\n", 1)[0] for t in tg.texts if not t.startswith(STARTED)]
    return others, events


def test_line_alerts_are_the_same_in_every_mode(tmp_path):
    runs = {m: line_alert_run(tmp_path, m) for m in ("auto", "always", "never")}
    others, events = runs["auto"]
    assert len(events) == 2 and len([t for t in others if t.startswith("🔔")]) == 2
    assert runs["always"] == runs["auto"] == runs["never"]


def test_indicator_alerts_are_the_same_in_every_mode(tmp_path):
    from tests.test_custom_indicators import wait_for_worker, write
    from tests.test_indicator_alerts_server import (
        LAST,
        a_level_that_crosses,
        alert_body,
        armed_back,
        ok,
        put_chart,
        run,
    )
    from tests.worker_helpers import FakeLauncher

    texts = {}
    for mode in ("auto", "always", "never"):
        h = Harness(tmp_path / mode, indicator_launchers=[FakeLauncher()])
        h.seed_candles()
        folder = h.config.data_dir / "indicators"
        folder.mkdir()
        gate = tmp_path / f"gate-{mode}"
        gate.mkdir()
        (gate / "go").write_text("")
        write(folder, "mine.py", f"GATE {gate}\n")
        tg = Telegram()
        h.server.telegram_transport = tg.transport
        h.secrets.set("telegram_token", TELEGRAM_TOKEN)
        with TestClient(create_app(h.server)) as c:
            c.server = h.server
            assert run_setup(c, telegram_chat_id="42").status_code == 200
            assert c.put("/api/settings", json={"telegram_start_message": mode}).status_code == 200
            wait_for_worker(c)
            put_chart(c, [{"uid": "m1", "kind": "custom", "id": "mine", "params": {}}])
            call(c, c.server.alerts.indicators.stop)
            c.server.alerts.indicators.evaluator.fresh = lambda _i, _t: True
            alert = ok(c.post("/api/alerts", json=alert_body(value=a_level_that_crosses(20))))
            live = armed_back(c, alert["id"], 20)
            live.notify = ["telegram", "app"]
            notice = run(c, LAST.ts + 65)
            assert notice.fires
            call(c, c.server.alerts.dispatcher.drain)
            texts[mode] = (
                [f.event.message for f in notice.fires],
                [t for t in tg.texts if not t.startswith(STARTED)],
            )
    assert texts["auto"] == texts["always"] == texts["never"]
    assert texts["auto"][0]


def test_catch_up_freeze_and_watchdog_messages_do_not_depend_on_the_mode(h):
    """They are sent by `dispatcher.system` from their own code: `never` keeps them."""
    import inspect

    from openchart.alerts import service

    src = inspect.getsource(service.AlertService)
    start_sends = [m for m in src.split("def ") if "alert engine started" in m]
    assert len(start_sends) == 1  # the start message is built in one place only
    assert "announce_start" in start_sends[0].split("(")[0]


# The stop paths ----------------------------------------------------------------------------


def run_state(data_dir):
    files = list(data_dir.glob("run-state*"))
    return json.loads(files[0].read_text()) if files else None


def start_process(data_dir, port):
    """`openchart run` as a process of its own on a scratch folder: no credentials, so no
    broker or Telegram request is made."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW  # a console of its own, as under the service
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            # Ctrl+C on again: a parent that ignores it (some shells and test runners) passes
            # that on to its children; a service manager's child does not have it.
            "import sys, ctypes; "
            "win = sys.platform == 'win32'; "
            "win and ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False); "
            "from openchart.main import main; sys.exit(main(sys.argv[1:]))",
            "run",
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
            "--host",
            "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    for _ in range(150):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise AssertionError("the server did not start")


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ctrl_c_on_its_console(pid: int) -> None:
    """Send Ctrl+C to the console of `pid`, from a helper process that attaches to it (as
    NSSM does), so the test's own console is not touched."""
    code = (
        "import ctypes, sys; k = ctypes.windll.kernel32; k.FreeConsole();"
        " assert k.AttachConsole(int(sys.argv[1])); k.SetConsoleCtrlHandler(None, True);"
        " assert k.GenerateConsoleCtrlEvent(0, 0)"
    )
    subprocess.run([sys.executable, "-c", code, str(pid)], check=True, timeout=30)


def test_a_stop_by_signal_notes_a_clean_stop_and_a_kill_does_not(tmp_path):
    """Ctrl+C (the Windows service's stop) and SIGTERM (`docker stop`) go through uvicorn's
    shutdown, which stops the server; a hard kill does not."""
    import signal

    port = free_port()
    proc = start_process(tmp_path / "a", port)
    assert run_state(tmp_path / "a")["state"] == "running"
    if sys.platform == "win32":
        ctrl_c_on_its_console(proc.pid)  # how NSSM stops the service (AppStopMethodConsole)
    else:
        proc.send_signal(signal.SIGTERM)  # `docker stop`
    proc.wait(timeout=30)  # uvicorn's own exit code (0, or 130 after Ctrl+C) is not the point
    assert run_state(tmp_path / "a")["state"] == "clean"

    proc = start_process(tmp_path / "b", free_port())
    proc.kill()
    proc.wait(timeout=30)
    assert run_state(tmp_path / "b")["state"] == "running"
