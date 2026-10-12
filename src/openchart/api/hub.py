"""WebSocket fan-out: ticks, live candles and feed status to connected clients."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from openchart import API_VERSION
from openchart.market.candle_builder import CandleBuilder, CandleUpdate
from openchart.market.timeframes import Timeframe, parse_timeframe
from openchart.providers.base import Candle, Tick

log = logging.getLogger(__name__)

QUEUE_SIZE = 2000  # a client this far behind is dropped; it reconnects and refetches


def candle_json(candle: Candle, price: str) -> dict[str, Any]:
    o, h, low, c = getattr(candle, price)
    return {"t": candle.ts, "o": o, "h": h, "l": low, "c": c, "v": candle.volume}


class Client:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(QUEUE_SIZE)
        # (instrument, timeframe, price) the client asked for
        self.subs: set[tuple[str, str, str]] = set()
        self.overflowed = False

    def send(self, message: dict[str, Any]) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)  # tells the sender loop to close the socket

    def instruments(self) -> set[str]:
        return {inst for inst, _, _ in self.subs}


class Hub:
    def __init__(self, builder: CandleBuilder, status=None):
        self.builder = builder
        self.status = status or (lambda: {})  # callable returning the status payload
        self.clients: set[Client] = set()
        builder.listeners.append(self.on_candle)

    def hello(self) -> dict[str, Any]:
        return {"type": "hello", "api_version": API_VERSION, "server_time": time.time()}

    def connect(self) -> Client:
        client = Client()
        self.clients.add(client)
        client.send(self.hello())
        client.send({"type": "status", **self.status()})
        return client

    def disconnect(self, client: Client) -> None:
        self.clients.discard(client)
        for inst, tf in {(i, t) for i, t, _ in client.subs}:
            self.builder.unsubscribe(inst, parse_timeframe(tf))
        client.subs.clear()

    async def subscribe(self, client: Client, instrument: str, tf: Timeframe, price: str) -> None:
        key = (instrument, tf.name, price)
        if key in client.subs:
            return
        first_for_tf = not any(i == instrument and t == tf.name for i, t, _ in client.subs)
        client.subs.add(key)
        if first_for_tf:
            await self.builder.subscribe(instrument, tf, time.time())
        current = self.builder.current(instrument, tf.name)
        if current is not None:
            client.send(self._candle_message(instrument, tf.name, price, current, False))

    def unsubscribe(self, client: Client, instrument: str, tf: Timeframe, price: str) -> None:
        key = (instrument, tf.name, price)
        if key not in client.subs:
            return
        client.subs.discard(key)
        if not any(i == instrument and t == tf.name for i, t, _ in client.subs):
            self.builder.unsubscribe(instrument, tf)

    # Fan-out

    def on_tick(self, tick: Tick) -> None:
        message = None
        for client in self.clients:
            if tick.instrument in client.instruments():
                message = message or {
                    "type": "tick",
                    "instrument": tick.instrument,
                    "t": tick.ts,
                    "bid": tick.bid,
                    "ask": tick.ask,
                }
                client.send(message)

    def on_candle(self, update: CandleUpdate) -> None:
        for client in self.clients:
            for inst, tf, price in client.subs:
                if inst == update.instrument and tf == update.timeframe:
                    client.send(self._candle_message(inst, tf, price, update.candle, update.closed))

    def broadcast_status(self) -> None:
        message = {"type": "status", **self.status()}
        for client in self.clients:
            client.send(message)

    @staticmethod
    def _candle_message(
        instrument: str, tf: str, price: str, candle: Candle, closed: bool
    ) -> dict[str, Any]:
        return {
            "type": "candle",
            "instrument": instrument,
            "timeframe": tf,
            "price": price,
            "candle": candle_json(candle, price),
            "closed": closed,
        }
