"""Setup wizard, login, settings and instrument management routes (SPEC section 8).

Secrets are write-only: requests may carry a token, responses only ever say whether a
token is set, where it comes from and its last four characters.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from openchart.alerts.notifiers.telegram import TelegramClient
from openchart.api.auth import SESSION_COOKIE, SESSION_SECONDS, PasswordError, check_new_password
from openchart.config import OandaConfig
from openchart.market.instruments import default_hours
from openchart.providers.base import InstrumentInfo, ProviderError
from openchart.server import Server
from openchart.settings import EncryptedFileSecrets, describe_secret

log = logging.getLogger(__name__)

Environment = Literal["practice", "live"]


class SetupBody(BaseModel):
    password: str
    oanda_environment: Environment = "practice"
    oanda_account_id: str = ""
    oanda_token: str = Field("", repr=False)
    telegram_token: str = Field("", repr=False)
    telegram_chat_id: str = ""
    instrument: str = "XAU_USD"
    setup_code: str = ""


class LoginBody(BaseModel):
    password: str = Field(repr=False)


class PasswordBody(BaseModel):
    current: str = Field(repr=False)
    new: str = Field(repr=False)


class SettingsUpdate(BaseModel):
    oanda_environment: Environment | None = None
    oanda_account_id: str | None = None
    oanda_token: str | None = Field(None, repr=False)  # empty or missing: keep
    telegram_token: str | None = Field(None, repr=False)
    telegram_chat_id: str | None = None
    clear_telegram: bool = False
    store_ticks: bool | None = None
    timezone: str | None = None
    listen_on_network: bool | None = None


class OandaTestBody(BaseModel):
    environment: Environment | None = None
    account_id: str | None = None
    token: str | None = Field(None, repr=False)


class TelegramTestBody(BaseModel):
    token: str | None = Field(None, repr=False)
    chat_id: str | None = None


class InstrumentBody(BaseModel):
    instrument: str


def client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def account_routes(server: Server) -> APIRouter:
    router = APIRouter(prefix="/api")
    setup_lock = asyncio.Lock()

    def need_secrets() -> None:
        if server.secrets is None:
            raise HTTPException(503, server.secrets_error or "No secret storage available")

    async def candidate_oanda(
        environment: str | None, account_id: str | None, token: str | None
    ) -> OandaConfig:
        current = (await server.effective_config()).oanda
        return OandaConfig(
            environment or current.environment,
            (token or "").strip() or current.token,
            (account_id or "").strip() or current.account_id,
        )

    async def refuse_feed_switch(new: OandaConfig) -> None:
        """Stored history belongs to one OANDA environment and account. Another one is a
        different price feed, and SPEC section 3 forbids mixing feeds in one series."""
        current = (await server.effective_config()).oanda
        if not current.account_id:
            return
        same = (new.environment, new.account_id) == (current.environment, current.account_id)
        if same or not await server.store.has_data(server.source):
            return
        raise HTTPException(
            409,
            f"The stored price history comes from the OANDA {current.environment} account "
            f"{current.account_id}. Switching to the {new.environment} account "
            f"{new.account_id} would mix two price feeds in the same charts and alerts, so "
            "it is refused. To use another account, start OpenChart with a new data folder "
            "(openchart run --data-dir <folder>) or move the current one aside. A new token "
            "for the same account is fine.",
        )

    async def oanda_instruments(oanda: OandaConfig) -> tuple[str, list[InstrumentInfo]]:
        """(source name, instruments of this account). Proves the token and account work."""
        if not oanda.configured:
            raise HTTPException(400, "Enter the OANDA account ID and API token.")
        provider = server.provider_factory(oanda)
        try:
            return provider.name, await provider.list_instruments()
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            await provider.aclose()

    async def enable_instrument(
        name: str, found: tuple[str, list[InstrumentInfo]]
    ) -> InstrumentInfo:
        source, available = found
        info = next((i for i in available if i.instrument == name), None)
        if info is None:
            raise HTTPException(
                400, f"{name} is not available on this account (names look like XAU_USD)."
            )
        hours = default_hours(info.instrument, info.kind)
        await server.store.upsert_instrument(source, info, hours)
        return info

    async def settings_payload() -> dict[str, Any]:
        app = await server.app_settings()
        env = server.config
        return {
            "oanda": {
                "environment": app.oanda_environment,
                "account_id": app.oanda_account_id or env.oanda.account_id,
                "token": describe_secret(server.secrets, "oanda_token", env.oanda.token),
            },
            "telegram": {
                "chat_id": app.telegram_chat_id,
                "token": describe_secret(server.secrets, "telegram_token"),
            },
            "store_ticks": app.store_ticks,
            "timezone": app.timezone,
            "listen_on_network": app.listen_on_network,
            "secret_store": server.secrets.backend if server.secrets else None,
            "secret_store_error": server.secrets_error,
        }

    # Setup and login

    @router.get("/setup/status")
    async def setup_status(request: Request) -> dict[str, Any]:
        configured = await server.auth.password_set()
        authenticated = await server.auth.session_valid(request.cookies.get(SESSION_COOKIE))
        return {
            "setup_required": not configured,
            "authenticated": configured and authenticated,
            "setup_code_required": not configured
            and not server.auth.setup_allowed_from(client_host(request), None),
            # The kind only: this route needs no login, so no paths.
            "secret_store": (
                "encrypted file"
                if isinstance(server.secrets, EncryptedFileSecrets)
                else server.secrets.backend
                if server.secrets
                else None
            ),
            "secret_store_error": server.secrets_error,
        }

    @router.get("/setup/defaults")
    async def setup_defaults() -> dict[str, Any]:
        """Values already known (from the environment) to prefill the wizard."""
        return await settings_payload()

    @router.post("/setup")
    async def setup(body: SetupBody, request: Request, response: Response) -> dict[str, Any]:
        async with setup_lock:
            if await server.auth.password_set():
                raise HTTPException(409, "OpenChart is already set up. Log in instead.")
            if not server.auth.setup_allowed_from(client_host(request), body.setup_code):
                raise HTTPException(403, "Wrong setup code. It is printed in the server log.")
            try:
                check_new_password(body.password)
            except PasswordError as exc:
                raise HTTPException(400, str(exc)) from None
            need_secrets()

            oanda = await candidate_oanda(
                body.oanda_environment, body.oanda_account_id, body.oanda_token
            )
            await refuse_feed_switch(oanda)
            found = await oanda_instruments(oanda)
            instrument = body.instrument.strip().upper()

            await server.settings.set("oanda_environment", oanda.environment)
            await server.settings.set("oanda_account_id", oanda.account_id)
            if body.oanda_token.strip():
                server.secrets.set("oanda_token", body.oanda_token.strip())
            if body.telegram_token.strip():
                server.secrets.set("telegram_token", body.telegram_token.strip())
            if body.telegram_chat_id.strip():
                await server.settings.set("telegram_chat_id", body.telegram_chat_id.strip())
            await enable_instrument(instrument, found)
            await server.auth.set_password(body.password)  # from here on, setup is done
            set_session_cookie(request, response, await server.auth.create_session())
            log.info("setup completed (%s account, %s)", oanda.environment, instrument)
        await server.reconfigure()
        return {"ok": True}

    @router.post("/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not await server.auth.password_set():
            raise HTTPException(409, "OpenChart is not set up yet.")
        who = client_host(request)
        wait = server.auth.limiter.retry_after(who)
        if wait > 0:
            raise HTTPException(
                429,
                f"Too many failed attempts. Try again in {int(wait // 60) + 1} min.",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        if not await server.auth.verify_password(body.password):
            server.auth.limiter.failed(who)
            log.warning("failed login from %s", who)
            raise HTTPException(401, "Wrong password.")
        server.auth.limiter.succeeded(who)
        set_session_cookie(request, response, await server.auth.create_session())
        return {"ok": True}

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        await server.auth.delete_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.post("/password")
    async def change_password(
        body: PasswordBody, request: Request, response: Response
    ) -> dict[str, Any]:
        who = client_host(request)
        if server.auth.limiter.retry_after(who) > 0:
            raise HTTPException(429, "Too many failed attempts. Try again later.")
        if not await server.auth.verify_password(body.current):
            server.auth.limiter.failed(who)
            raise HTTPException(400, "The current password is wrong.")
        try:
            await server.auth.set_password(body.new)  # also logs out every other device
        except PasswordError as exc:
            raise HTTPException(400, str(exc)) from None
        set_session_cookie(request, response, await server.auth.create_session())
        return {"ok": True}

    # Settings

    @router.get("/settings")
    async def get_settings() -> dict[str, Any]:
        return await settings_payload()

    @router.put("/settings")
    async def put_settings(body: SettingsUpdate) -> dict[str, Any]:
        app = await server.app_settings()
        reconfigure = False
        restart_required = False

        oanda_changed = (
            (body.oanda_environment not in (None, app.oanda_environment))
            or (body.oanda_account_id not in (None, "", app.oanda_account_id))
            or bool((body.oanda_token or "").strip())
        )
        if oanda_changed:
            if (body.oanda_token or "").strip():
                need_secrets()
            oanda = await candidate_oanda(
                body.oanda_environment, body.oanda_account_id, body.oanda_token
            )
            await refuse_feed_switch(oanda)
            await oanda_instruments(oanda)  # refuse to save credentials that do not work
            await server.settings.set("oanda_environment", oanda.environment)
            await server.settings.set("oanda_account_id", oanda.account_id)
            if (body.oanda_token or "").strip():
                server.secrets.set("oanda_token", body.oanda_token.strip())
            reconfigure = True

        if body.clear_telegram:
            need_secrets()
            server.secrets.delete("telegram_token")
            await server.settings.delete("telegram_chat_id")
        else:
            if (body.telegram_token or "").strip():
                need_secrets()
                server.secrets.set("telegram_token", body.telegram_token.strip())
            if body.telegram_chat_id is not None:
                await server.settings.set("telegram_chat_id", body.telegram_chat_id.strip())

        if body.store_ticks is not None and body.store_ticks != app.store_ticks:
            await server.settings.set("store_ticks", body.store_ticks)
            reconfigure = True
        if body.timezone is not None:
            await server.settings.set("timezone", body.timezone)
        if body.listen_on_network is not None and body.listen_on_network != app.listen_on_network:
            await server.settings.set("listen_on_network", body.listen_on_network)
            restart_required = True

        if reconfigure:
            await server.reconfigure()
        return {**(await settings_payload()), "restart_required": restart_required}

    @router.post("/settings/test/oanda")
    async def test_oanda(body: OandaTestBody) -> dict[str, Any]:
        oanda = await candidate_oanda(body.environment, body.account_id, body.token)
        if not oanda.configured:
            return {"ok": False, "message": "Enter the OANDA account ID and API token."}
        provider = server.provider_factory(oanda)
        try:
            result = await provider.test_connection()
        finally:
            await provider.aclose()
        return {"ok": result.ok, "message": result.message}

    @router.post("/settings/test/telegram")
    async def test_telegram(body: TelegramTestBody) -> dict[str, Any]:
        stored = server.secrets.get("telegram_token") if server.secrets else None
        token = (body.token or "").strip() or stored or ""
        chat_id = (body.chat_id or "").strip() or (await server.app_settings()).telegram_chat_id
        client = TelegramClient(token, chat_id, transport=server.telegram_transport)
        result = await client.test()
        return {"ok": result.ok, "message": result.message}

    # Instruments

    @router.get("/instruments/available")
    async def available_instruments(q: str = "") -> list[dict[str, Any]]:
        oanda = (await server.effective_config()).oanda
        _, available = await oanda_instruments(oanda)
        enabled = {r.instrument for r in await server.store.instruments(enabled_only=True)}
        needle = q.strip().upper().replace("/", "_")
        return [
            {
                "instrument": i.instrument,
                "display_name": i.display_name,
                "kind": i.kind,
                "enabled": i.instrument in enabled,
            }
            # Names that start with the search first (EUR_USD before DE30_EUR).
            for i in sorted(
                available, key=lambda i: (not i.instrument.startswith(needle), i.instrument)
            )
            if not needle or needle in i.instrument or needle in i.display_name.upper()
        ]

    @router.post("/instruments")
    async def add_instrument(body: InstrumentBody) -> dict[str, Any]:
        oanda = (await server.effective_config()).oanda
        info = await enable_instrument(
            body.instrument.strip().upper(), await oanda_instruments(oanda)
        )
        await server.reconfigure()  # the feed backfills new instruments when it starts
        return {"ok": True, "instrument": info.instrument}

    @router.delete("/instruments/{instrument}")
    async def remove_instrument(instrument: str) -> dict[str, Any]:
        if not await server.store.set_instrument_enabled(server.source, instrument, False):
            raise HTTPException(404, f"unknown instrument {instrument!r}")
        await server.reconfigure()
        return {"ok": True}

    return router
