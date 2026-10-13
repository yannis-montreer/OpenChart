"""Settings and secrets.

Non-secret settings live in the `settings` table (JSON values). Secrets (the OANDA and
Telegram tokens) live in the OS credential store through `keyring` (Windows Credential
Manager, macOS Keychain, Secret Service on Linux). Where no credential store exists
(Docker, headless Linux) they go to an encrypted file in the data folder, with the key
taken from the OPENCHART_SECRET_KEY environment variable.

Secrets are never written to the database, never logged and never returned by the API:
only `mask_secret()` forms leave this module.

Each database has its own `instance_id`, used to namespace credential-store entries, so
two installs (or a test copy) on one machine never share or overwrite secrets.

Environment variables from M1 (`OPENCHART_OANDA_*`) still work as a fallback, for
headless installs; anything saved through the UI takes precedence.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets as pysecrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from openchart.config import OANDA_HOSTS, Config, OandaConfig, mask_secret

log = logging.getLogger(__name__)

SECRET_NAMES = ("oanda_token", "telegram_token")
SECRET_KEY_ENV = "OPENCHART_SECRET_KEY"


class SecretStoreUnavailable(RuntimeError):
    pass


class Secrets(Protocol):
    backend: str

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class KeyringSecrets:
    """OS credential store. One entry per secret, service `OpenChart/<instance_id>`."""

    def __init__(self, instance_id: str):
        import keyring

        self._keyring = keyring
        self.service = f"OpenChart/{instance_id}"
        kr = keyring.get_keyring()
        self.backend = f"{type(kr).__name__}"

    @staticmethod
    def available() -> bool:
        try:
            import keyring
            from keyring.backends import fail

            kr = keyring.get_keyring()
            return not isinstance(kr, fail.Keyring) and getattr(kr, "priority", 1) > 0
        except Exception:
            return False

    def get(self, name: str) -> str | None:
        return self._keyring.get_password(self.service, name)

    def set(self, name: str, value: str) -> None:
        self._keyring.set_password(self.service, name, value)

    def delete(self, name: str) -> None:
        with contextlib.suppress(self._keyring.errors.PasswordDeleteError):
            self._keyring.delete_password(self.service, name)


class EncryptedFileSecrets:
    """Fernet-encrypted JSON file; the key comes from the environment, never from disk."""

    backend = "encrypted file"

    def __init__(self, path: Path, key: str):
        from cryptography.fernet import Fernet, InvalidToken

        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise SecretStoreUnavailable(
                f"{SECRET_KEY_ENV} is not a valid key. Create one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
        self._invalid = InvalidToken
        self.path = path

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except self._invalid as exc:
            raise SecretStoreUnavailable(
                f"Cannot decrypt {self.path.name}: {SECRET_KEY_ENV} has changed"
            ) from exc

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(self._fernet.encrypt(json.dumps(data).encode()))
        os.replace(tmp, self.path)

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)


class MemorySecrets:
    """For tests."""

    backend = "memory"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def open_secrets(config: Config, instance_id: str) -> Secrets:
    key = os.environ.get(SECRET_KEY_ENV)
    if key:  # an explicit key means the user wants the file store (e.g. Docker)
        return EncryptedFileSecrets(config.data_dir / "secrets.enc", key)
    if KeyringSecrets.available():
        return KeyringSecrets(instance_id)
    raise SecretStoreUnavailable(
        "No OS credential store is available here. Set the environment variable "
        f"{SECRET_KEY_ENV} to a Fernet key to store secrets in an encrypted file."
    )


class SettingsStore:
    """Key/value settings in SQLite. Values are JSON. Never put secrets here."""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def get(self, key: str, default: Any = None) -> Any:
        async with self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return default if row is None else json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        if key in SECRET_NAMES:
            raise ValueError(f"{key} is a secret and does not belong in the settings table")
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await self.conn.commit()

    async def delete(self, key: str) -> None:
        await self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        await self.conn.commit()

    async def instance_id(self) -> str:
        value = await self.get("instance_id")
        if value is None:
            value = pysecrets.token_hex(8)
            await self.set("instance_id", value)
        return value


@dataclass(frozen=True)
class AppSettings:
    """Everything the settings page edits, except secrets and the password."""

    oanda_environment: str
    oanda_account_id: str
    telegram_chat_id: str
    store_ticks: bool
    timezone: str
    listen_on_network: bool


async def load_app_settings(store: SettingsStore, config: Config) -> AppSettings:
    return AppSettings(
        oanda_environment=await store.get("oanda_environment", config.oanda.environment),
        oanda_account_id=await store.get("oanda_account_id", config.oanda.account_id),
        telegram_chat_id=await store.get("telegram_chat_id", ""),
        store_ticks=await store.get("store_ticks", config.store_ticks),
        timezone=await store.get("timezone", ""),
        listen_on_network=await store.get("listen_on_network", config.host != "127.0.0.1"),
    )


def effective_oanda(config: Config, app: AppSettings, secrets: Secrets | None) -> OandaConfig:
    """OANDA settings: saved through the UI first, environment as a fallback."""
    token = (secrets.get("oanda_token") if secrets else None) or config.oanda.token
    environment = app.oanda_environment if app.oanda_environment in OANDA_HOSTS else "practice"
    return OandaConfig(environment, token, app.oanda_account_id or config.oanda.account_id)


def effective_config(config: Config, app: AppSettings, secrets: Secrets | None) -> Config:
    return replace(config, oanda=effective_oanda(config, app, secrets), store_ticks=app.store_ticks)


def describe_secret(secrets: Secrets | None, name: str, env_value: str = "") -> dict[str, Any]:
    """What the API may say about a secret: whether it is set, where from, last 4 chars."""
    stored = secrets.get(name) if secrets else None
    value = stored or env_value
    return {
        "set": bool(value),
        "masked": mask_secret(value),
        "source": "saved" if stored else ("environment" if env_value else None),
    }
