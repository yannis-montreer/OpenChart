"""Telegram Bot API client (sendMessage).

The bot token is part of the request URL, so errors are rebuilt from Telegram's JSON
answer or the exception type and never include the URL or the token.
"""

from __future__ import annotations

import httpx

from openchart.providers.base import TestResult

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ):
        self._token = token
        self.chat_id = str(chat_id).strip()
        self._transport = transport
        self._timeout = timeout

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "****") if self._token else text

    async def send(self, text: str) -> None:
        if not self._token or not self.chat_id:
            raise TelegramError("Telegram bot token and chat ID are both needed")
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
                resp = await http.post(
                    f"{API}/bot{self._token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                )
        except httpx.HTTPError as exc:
            raise TelegramError(f"Could not reach Telegram ({type(exc).__name__})") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            description = body.get("description") or f"HTTP {resp.status_code}"
            hint = {
                401: " Check the bot token.",
                404: " Check the bot token.",
                400: " Check the chat ID, and send your bot a message first.",
                403: " Send your bot a message first (or unblock it).",
            }.get(resp.status_code, "")
            raise TelegramError(self._clean(f"Telegram refused the message: {description}.{hint}"))

    async def test(self) -> TestResult:
        try:
            await self.send("OpenChart: test message. Alerts will arrive here.")
        except TelegramError as exc:
            return TestResult(False, self._clean(str(exc)))
        return TestResult(True, "Test message sent. Check Telegram.")
