"""Notifications (SPEC section 9): message text, notifier interface, delivery with retries.

Telegram is the first notifier; ntfy or Pushover would implement the same `Notifier`.
Alert messages are recorded in `alert_events` and retried until delivered
(`alert_events.delivered`); system messages (watchdog, freeze) are retried in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openchart.alerts.notifiers.telegram import TelegramClient, TelegramError
from openchart.store.alerts import Alert, AlertEvent, AlertStore

log = logging.getLogger(__name__)

# Seconds before each retry; after the last one the message is given up (still logged).
RETRY_DELAYS = (5, 30, 120, 600, 1800, 3600)
LATE_TEXT = {
    "wake": "detected after wake-up (the server was frozen, the computer probably slept)",
    "reconnect": "detected after the price feed reconnected",
    "restart": "detected after a server restart",
}


class NotifierUnavailable(Exception):
    """Not set up: retrying will not help."""


class Notifier(Protocol):
    name: str

    async def send(self, text: str) -> None: ...


class TelegramNotifier:
    name = "telegram"

    def __init__(self, client: TelegramClient):
        self.client = client

    async def send(self, text: str) -> None:
        await self.client.send(text)


def zone(name: str) -> tzinfo:
    """The display timezone from Settings, or the server's local one."""
    if name:
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or UTC


def fmt_time(ts: float, tz: tzinfo) -> str:
    d = datetime.fromtimestamp(ts, tz)
    return f"{d:%a %d %b %Y %H:%M:%S} {d.tzname() or ''}".strip()


def fmt_price(value: float | None, decimals: int) -> str:
    return "-" if value is None else f"{value:,.{decimals}f}"


def default_name(alert: Alert, line_type: str | None, decimals: int) -> str:
    what = {"up": "Crossing Up", "down": "Crossing Down"}.get(alert.direction, "Crossing")
    target = (
        {"hline": "horizontal line", "trendline": "trend line", "extended": "extended line"}.get(
            line_type or "", line_type or "line"
        )
        if alert.type == "line_cross"
        else f"{alert.level:.{decimals}f}"
    )
    return f"{alert.instrument}, {alert.timeframe} {what} {target}"


def alert_text(alert: Alert, event: AlertEvent, decimals: int, tz: tzinfo) -> str:
    arrow = "up" if event.cross == "up" else "down"
    lines = [
        f"🔔 {alert.name or alert.instrument}",
        f"{alert.instrument} crossed {arrow}: {fmt_price(event.price, decimals)} "
        f"({alert.trigger_price}), line {fmt_price(event.line_value, decimals)}",
        fmt_time(event.ts, tz),
    ]
    if alert.trigger_mode == "candle_close":
        lines[1] += f", on the {alert.timeframe} close"
    if event.late:
        lines.append(
            f"⚠️ Late: {LATE_TEXT.get(event.late, event.late)}, at {fmt_time(event.detected_at, tz)}"
        )
    return "\n".join(lines)


class Dispatcher:
    """Sends messages in the background and retries failures."""

    def __init__(
        self,
        store: AlertStore,
        notifier: Callable[[], Awaitable[Notifier | None]],
        *,
        delays: Sequence[float] = RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.store = store
        self.notifier = notifier
        self.delays = delays
        self.sleep = sleep
        self.tasks: set[asyncio.Task[None]] = set()
        self.on_delivery: Callable[[int], None] | None = None  # event id, after a change

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def event(self, event: AlertEvent, text: str) -> None:
        self._spawn(self._event(event, text))

    def system(self, text: str) -> None:
        self._spawn(self._system(text))

    async def _send(self, text: str) -> None:
        notifier = await self.notifier()
        if notifier is None:
            raise NotifierUnavailable("Telegram is not set up (Settings, Telegram alerts)")
        await notifier.send(text)

    async def _event(self, event: AlertEvent, text: str) -> None:
        attempts = event.attempts
        for delay in (0, *self.delays):
            if delay:
                await self.sleep(delay)
            attempts += 1
            try:
                await self._send(text)
            except NotifierUnavailable as exc:
                await self._record(event, False, attempts, str(exc))
                return
            except (TelegramError, OSError) as exc:
                log.warning(
                    "alert %s not delivered (attempt %d): %s", event.alert_id, attempts, exc
                )
                await self._record(event, False, attempts, str(exc))
                continue
            await self._record(event, True, attempts, None)
            return

    async def _record(
        self, event: AlertEvent, delivered: bool, attempts: int, error: str | None
    ) -> None:
        event.delivered, event.attempts, event.error = delivered, attempts, error
        await self.store.set_delivery(event.id, delivered, attempts, error)
        if self.on_delivery:
            self.on_delivery(event.id)

    async def _system(self, text: str) -> None:
        for delay in (0, *self.delays):
            if delay:
                await self.sleep(delay)
            try:
                await self._send(text)
                return
            except NotifierUnavailable:
                log.info("not sent (Telegram is not set up): %s", text.splitlines()[0])
                return
            except (TelegramError, OSError) as exc:
                log.warning("system message not delivered: %s", exc)

    async def drain(self) -> None:
        """Wait for the messages in flight (tests)."""
        # Only tasks still running: one that is done but not yet (or never) discarded would
        # keep `while self.tasks` true and this loop spinning for ever.
        while pending := [t for t in self.tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
