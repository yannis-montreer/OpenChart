"""Reconnecting tick stream on top of any `Provider`.

Handles what every provider needs: heartbeat supervision (no event for
`heartbeat_timeout` seconds means the link is dead), reconnect with exponential
backoff and jitter, and a hook that runs once each connection is up, used to
gap-fill candles from REST after an outage.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum

from openchart.providers.base import AuthError, Provider, Tick

log = logging.getLogger(__name__)


class StreamState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


OnConnected = Callable[[bool], Awaitable[None]]  # argument: True after a reconnect
OnState = Callable[[StreamState, str], None]


class ResilientTickStream:
    def __init__(
        self,
        provider: Provider,
        instruments: list[str],
        *,
        heartbeat_timeout: float = 15.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        on_connected: OnConnected | None = None,
        on_state: OnState | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.provider = provider
        self.instruments = instruments
        self.heartbeat_timeout = heartbeat_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.on_connected = on_connected
        self.on_state = on_state
        self._sleep = sleep
        self.state = StreamState.STOPPED
        self.reconnects = 0
        self.last_event_at: float | None = None  # monotonic

    def _set_state(self, state: StreamState, detail: str = "") -> None:
        if state != self.state:
            self.state = state
            log.info("stream %s%s", state.value, f" ({detail})" if detail else "")
            if self.on_state:
                self.on_state(state, detail)

    def backoff_delay(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_initial * 2**attempt)
        return base * (0.5 + random.random() / 2)

    async def ticks(self) -> AsyncIterator[Tick]:
        attempt = 0
        connected_before = False
        try:
            while True:
                state = StreamState.RECONNECTING if connected_before else StreamState.CONNECTING
                self._set_state(state)
                events = aiter(self.provider.stream_ticks(self.instruments))
                up = False
                try:
                    while True:
                        async with asyncio.timeout(self.heartbeat_timeout):
                            event = await anext(events)
                        self.last_event_at = time.monotonic()
                        if not up:
                            up, attempt = True, 0
                            self._set_state(StreamState.CONNECTED)
                            if connected_before:
                                self.reconnects += 1
                            if self.on_connected:
                                try:
                                    await self.on_connected(connected_before)
                                except Exception:
                                    # A failed gap-fill must not take the live feed down.
                                    log.exception("on_connected hook failed")
                            connected_before = True
                        if isinstance(event, Tick):
                            yield event
                except StopAsyncIteration:
                    reason = "stream closed by server"
                except TimeoutError:
                    reason = f"no data for {self.heartbeat_timeout:.0f}s"
                except AuthError:
                    raise
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                finally:
                    aclose = getattr(events, "aclose", None)
                    if aclose is not None:
                        await aclose()
                delay = self.backoff_delay(attempt)
                attempt += 1
                log.warning("stream lost (%s), reconnecting in %.1fs", reason, delay)
                self._set_state(StreamState.RECONNECTING, reason)
                await self._sleep(delay)
        finally:
            self._set_state(StreamState.STOPPED)
