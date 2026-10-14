"""Setup, login, sessions, settings and secrets (SPEC sections 8 and 17)."""

import asyncio
import json
import logging

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.websockets import WebSocketDisconnect

from openchart.alerts.notifiers.telegram import TelegramClient
from openchart.api.auth import SESSION_COOKIE, LoginLimiter
from openchart.config import Config
from openchart.settings import (
    EncryptedFileSecrets,
    SecretStoreUnavailable,
    SettingsStore,
    open_secrets,
)
from openchart.store.sqlite import Database
from tests.conftest import PASSWORD, SETUP, TOKEN, run_setup
from tests.ws import receive

TELEGRAM_TOKEN = "123456:TELEGRAM-secret-token-9876"


# Before setup


def test_fresh_server_asks_for_setup(fresh):
    status = fresh.get("/api/setup/status").json()
    assert status["setup_required"] is True
    assert status["authenticated"] is False
    # The test client is not this computer, so the printed setup code is needed.
    assert status["setup_code_required"] is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/candles?instrument=XAU_USD",
        "/api/status",
        "/api/settings",
        "/api/version",
        "/api/meta",
        "/api/instruments",
    ],
)
def test_everything_else_needs_a_session(fresh, path):
    assert fresh.get(path).status_code == 401


def test_websocket_needs_a_session(fresh):
    with fresh.websocket_connect("/ws") as ws, pytest.raises(WebSocketDisconnect) as exc:
        receive(ws)
    assert exc.value.code == 4401


def test_setup_needs_the_code_from_the_log(fresh):
    response = fresh.post("/api/setup", json={**SETUP, "setup_code": "WRONG1"})
    assert response.status_code == 403
    assert fresh.get("/api/setup/status").json()["setup_required"] is True


def test_setup_rejects_short_password(fresh):
    assert run_setup(fresh, password="short").status_code == 400


def test_setup_refuses_credentials_that_do_not_work(fresh):
    response = run_setup(fresh, oanda_token="wrong-token")
    assert response.status_code == 400
    assert "Insufficient authorization" in response.json()["detail"]
    assert fresh.get("/api/setup/status").json()["setup_required"] is True
    assert fresh.harness.secrets.values == {}  # nothing saved


def test_setup_refuses_unknown_instrument(fresh):
    response = run_setup(fresh, instrument="NOPE_USD")
    assert response.status_code == 400
    assert fresh.get("/api/setup/status").json()["setup_required"] is True


def test_test_buttons_work_before_setup_with_the_code(fresh):
    code = {"X-Setup-Code": fresh.server.auth.setup_code}
    body = {"environment": "practice", "account_id": "101-001-1-001", "token": TOKEN}
    assert fresh.post("/api/settings/test/oanda", json=body).status_code == 401
    ok = fresh.post("/api/settings/test/oanda", json=body, headers=code).json()
    assert ok["ok"] is True
    bad = fresh.post(
        "/api/settings/test/oanda", json={**body, "token": "nope"}, headers=code
    ).json()
    assert bad["ok"] is False and "Insufficient authorization" in bad["message"]


def test_setup_completes_once_and_logs_in(fresh):
    response = run_setup(fresh, telegram_token=TELEGRAM_TOKEN, telegram_chat_id="42")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Max-Age=2592000" in cookie  # 30 days

    assert fresh.get("/api/setup/status").json() == {
        "setup_required": False,
        "authenticated": True,
        "setup_code_required": False,
        "secret_store": "memory",
        "secret_store_error": None,
    }
    assert fresh.harness.secrets.values == {"oanda_token": TOKEN, "telegram_token": TELEGRAM_TOKEN}
    assert [i["instrument"] for i in fresh.get("/api/instruments").json()] == ["XAU_USD"]
    assert fresh.server.feed is not None  # the feed started without a restart

    # Setup only works once, even for a logged-in client.
    assert run_setup(fresh, password="another password").status_code == 409


# Login


def test_login_logout(client):
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"password": "wrong password"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/status").status_code == 401


def test_login_is_rate_limited(client):
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/api/login", json={"password": "guess"}).status_code == 401
    blocked = client.post("/api/login", json={"password": PASSWORD})
    assert blocked.status_code == 429  # even the right password waits
    assert int(blocked.headers["Retry-After"]) > 60


def test_limiter_window_and_reset():
    now = [0.0]
    limiter = LoginLimiter(per_client=3, overall=5, window=60, clock=lambda: now[0])
    for _ in range(3):
        limiter.failed("a")
    assert limiter.retry_after("a") == pytest.approx(60)
    assert limiter.retry_after("b") == 0  # other clients are not affected...
    limiter.failed("b")
    limiter.failed("c")
    assert limiter.retry_after("d") == pytest.approx(60)  # ...until the global cap is hit
    now[0] = 61
    assert limiter.retry_after("a") == 0
    limiter.failed("a")
    limiter.succeeded("a")
    assert limiter.retry_after("a") == 0


def test_password_change_logs_out_other_devices(client):
    other_device = client.cookies.get(SESSION_COOKIE)
    wrong = client.post("/api/password", json={"current": "nope nope", "new": "brand new pass"})
    assert wrong.status_code == 400
    ok = client.post("/api/password", json={"current": PASSWORD, "new": "brand new pass"})
    assert ok.status_code == 200
    assert client.get("/api/status").status_code == 200  # this browser stays logged in
    client.cookies.set(SESSION_COOKIE, other_device)
    assert client.get("/api/status").status_code == 401  # the old session is gone
    client.cookies.clear()
    assert client.post("/api/login", json={"password": "brand new pass"}).status_code == 200


def test_cross_site_requests_are_refused(client):
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/logout", headers=evil).status_code == 403
    assert client.put("/api/settings", json={"timezone": "UTC"}, headers=evil).status_code == 403
    same = {"Origin": "http://testserver"}
    assert client.put("/api/settings", json={"timezone": "UTC"}, headers=same).status_code == 200
    with (
        client.websocket_connect("/ws", headers=evil) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        receive(ws)
    assert exc.value.code == 4401


# Settings and secrets


def test_settings_show_secrets_masked(client):
    client.put("/api/settings", json={"telegram_token": TELEGRAM_TOKEN, "telegram_chat_id": "42"})
    settings = client.get("/api/settings").json()
    assert settings["oanda"]["token"] == {"set": True, "masked": "****1234", "source": "saved"}
    assert settings["telegram"]["token"]["masked"] == "****9876"
    assert settings["telegram"]["chat_id"] == "42"
    assert settings["secret_store"] == "memory"


def test_secrets_never_leave_the_server(client, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [
        client.put(
            "/api/settings", json={"telegram_token": TELEGRAM_TOKEN, "telegram_chat_id": "1"}
        ),
        client.put("/api/settings", json={"oanda_token": TOKEN}),
        client.post("/api/settings/test/oanda", json={}),
    ]
    for path in [
        "/api/settings",
        "/api/setup/status",
        "/api/setup/defaults",
        "/api/status",
        "/api/instruments",
        "/api/meta",
        "/api/version",
        "/api/candles?instrument=XAU_USD&limit=3",
        "/api/instruments/available",
        "/openapi.json",
    ]:
        responses.append(client.get(path))
    for response in responses:
        assert response.status_code == 200, response.request.url
        assert TOKEN not in response.text and TELEGRAM_TOKEN not in response.text
    assert TOKEN not in caplog.text and TELEGRAM_TOKEN not in caplog.text
    # The settings table never holds a secret either.
    rows = client.portal.call(
        client.server.db.conn.execute_fetchall, "SELECT key, value FROM settings"
    )
    assert TOKEN not in json.dumps(rows) and TELEGRAM_TOKEN not in json.dumps(rows)


def test_rotating_the_oanda_token_is_checked_first(client):
    calls = len(client.harness.factory_calls)
    bad = client.put("/api/settings", json={"oanda_token": "expired-token"})
    assert bad.status_code == 400
    assert client.harness.secrets.values["oanda_token"] == TOKEN  # unchanged
    good = client.put("/api/settings", json={"oanda_token": TOKEN})
    assert good.status_code == 200
    assert client.harness.secrets.values["oanda_token"] == TOKEN
    assert len(client.harness.factory_calls) > calls


@pytest.mark.parametrize(
    "change",
    [
        {"oanda_environment": "live"},
        {"oanda_account_id": "101-001-9-001"},
        {"oanda_environment": "live", "oanda_account_id": "001-001-9-001", "oanda_token": TOKEN},
    ],
)
def test_switching_oanda_account_with_stored_data_is_refused(client, change):
    calls = len(client.harness.factory_calls)
    out = client.put("/api/settings", json=change)
    assert out.status_code == 409
    assert "mix two price feeds" in out.json()["detail"]
    assert "101-001-1-001" in out.json()["detail"]  # says which account the data is from
    assert client.get("/api/settings").json()["oanda"]["account_id"] == "101-001-1-001"
    assert len(client.harness.factory_calls) == calls  # refused before any broker call


def test_switching_oanda_account_without_stored_data_is_allowed(harness):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app

    harness.server.run_feed = False  # nothing gets stored
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        out = c.put("/api/settings", json={"oanda_environment": "live", "oanda_token": TOKEN})
        assert out.status_code == 200
        assert out.json()["oanda"]["environment"] == "live"


def test_the_wizard_refuses_another_account_for_existing_data(harness):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app

    harness.seed_candles()

    async def saved():
        async with await Database.open(harness.config.db_path) as db:
            settings = SettingsStore(db.conn)
            await settings.set("oanda_environment", "practice")
            await settings.set("oanda_account_id", "101-001-1-001")

    asyncio.run(saved())
    with TestClient(create_app(harness.server)) as c:  # no password yet: the wizard
        c.server = harness.server
        out = run_setup(c, oanda_environment="live", oanda_account_id="001-001-1-001")
        assert out.status_code == 409 and "mix two price feeds" in out.json()["detail"]
        assert run_setup(c).status_code == 200  # the same account is fine


def test_clear_telegram_and_other_settings(client):
    client.put("/api/settings", json={"telegram_token": TELEGRAM_TOKEN, "telegram_chat_id": "42"})
    out = client.put(
        "/api/settings",
        json={
            "clear_telegram": True,
            "store_ticks": False,
            "timezone": "Europe/Oslo",
            "listen_on_network": True,
        },
    ).json()
    assert out["telegram"]["token"]["set"] is False and out["telegram"]["chat_id"] == ""
    assert out["store_ticks"] is False and client.server.tick_writer is None
    assert out["timezone"] == "Europe/Oslo"
    assert out["listen_on_network"] is True and out["restart_required"] is True


def test_add_and_remove_instruments(client):
    available = client.get("/api/instruments/available?q=eur").json()
    assert available == [
        {"instrument": "EUR_USD", "display_name": "EUR/USD", "kind": "CURRENCY", "enabled": False}
    ]
    assert client.post("/api/instruments", json={"instrument": "eur_usd"}).status_code == 200
    names = {i["instrument"]: i["enabled"] for i in client.get("/api/instruments").json()}
    assert names == {"EUR_USD": True, "XAU_USD": True}
    assert set(client.server.feed.instruments) == {"EUR_USD", "XAU_USD"}

    assert client.delete("/api/instruments/EUR_USD").status_code == 200
    names = {i["instrument"]: i["enabled"] for i in client.get("/api/instruments").json()}
    assert names["EUR_USD"] is False
    assert client.server.feed.instruments == ["XAU_USD"]
    assert client.post("/api/instruments", json={"instrument": "NOPE"}).status_code == 400
    assert client.delete("/api/instruments/NOPE").status_code == 404


# Telegram


def telegram_transport(status: int, body: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_telegram_sends_message():
    seen: list = []
    client = TelegramClient(
        TELEGRAM_TOKEN, "42", transport=telegram_transport(200, {"ok": True}, seen)
    )
    result = await client.test()
    assert result.ok
    assert json.loads(seen[0].content)["chat_id"] == "42"


async def test_telegram_errors_never_show_the_token():
    seen: list = []
    body = {"ok": False, "description": f"Unauthorized for bot{TELEGRAM_TOKEN}"}
    client = TelegramClient(TELEGRAM_TOKEN, "42", transport=telegram_transport(401, body, seen))
    result = await client.test()
    assert not result.ok
    assert TELEGRAM_TOKEN not in result.message and "Check the bot token" in result.message

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    result = await TelegramClient(TELEGRAM_TOKEN, "42", transport=httpx.MockTransport(boom)).test()
    assert not result.ok and TELEGRAM_TOKEN not in result.message


def test_telegram_test_button(client):
    seen: list = []
    client.server.telegram_transport = telegram_transport(200, {"ok": True}, seen)
    out = client.post(
        "/api/settings/test/telegram", json={"token": TELEGRAM_TOKEN, "chat_id": "7"}
    ).json()
    assert out == {"ok": True, "message": "Test message sent. Check Telegram."}


# Secret stores


def test_encrypted_file_store(tmp_path):
    key = Fernet.generate_key().decode()
    store = EncryptedFileSecrets(tmp_path / "secrets.enc", key)
    store.set("oanda_token", TOKEN)
    assert TOKEN.encode() not in (tmp_path / "secrets.enc").read_bytes()
    assert EncryptedFileSecrets(tmp_path / "secrets.enc", key).get("oanda_token") == TOKEN
    store.delete("oanda_token")
    assert store.get("oanda_token") is None

    store.set("oanda_token", TOKEN)
    other = EncryptedFileSecrets(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    with pytest.raises(SecretStoreUnavailable, match="has changed"):
        other.get("oanda_token")
    with pytest.raises(SecretStoreUnavailable, match="not a valid key"):
        EncryptedFileSecrets(tmp_path / "x.enc", "not-a-key")


def test_secret_key_env_selects_the_encrypted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCHART_SECRET_KEY", Fernet.generate_key().decode())
    store = open_secrets(Config(data_dir=tmp_path), "abc")
    assert isinstance(store, EncryptedFileSecrets)


async def test_settings_table_refuses_secrets(tmp_path):
    from openchart.store.sqlite import Database

    async with await Database.open(tmp_path / "t.db") as db:
        store = SettingsStore(db.conn)
        with pytest.raises(ValueError):
            await store.set("oanda_token", TOKEN)
        first = await store.instance_id()
        assert first == await store.instance_id()


def test_os_credential_store_round_trip():
    """Uses the real OS store (Windows Credential Manager here) with a throwaway entry."""
    import secrets as pysecrets

    from openchart.settings import KeyringSecrets

    if not KeyringSecrets.available():
        pytest.skip("no OS credential store on this machine")
    store = KeyringSecrets(f"test-{pysecrets.token_hex(6)}")
    try:
        assert store.get("oanda_token") is None
        store.set("oanda_token", "dummy-value-1234")
        assert store.get("oanda_token") == "dummy-value-1234"
    finally:
        store.delete("oanda_token")
    assert store.get("oanda_token") is None
    store.delete("oanda_token")  # deleting twice is fine
