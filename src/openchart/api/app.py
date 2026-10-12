"""FastAPI application: REST routes and the /ws websocket.

Authentication arrives in M4. Until then the server binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from openchart import API_VERSION, __version__
from openchart.api.hub import Client
from openchart.market.aggregate import PRICES
from openchart.market.timeframes import TIMEFRAMES, parse_timeframe
from openchart.server import Server


def create_app(server: Server) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await server.start()
        try:
            yield
        finally:
            await server.stop()

    app = FastAPI(title="OpenChart", version=__version__, lifespan=lifespan)
    app.state.server = server

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/api/version")
    async def version() -> dict[str, Any]:
        return {"version": __version__, "api_version": API_VERSION}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return server.status()

    @app.get("/api/instruments")
    async def instruments() -> list[dict[str, Any]]:
        rows = await server.store.instruments(server.source)
        return [
            {
                "instrument": r.instrument,
                "source": r.source,
                "provider_symbol": r.provider_symbol,
                "display_name": r.display_name,
                "decimals": r.decimals,
                "pip_size": r.pip_size,
                "enabled": r.enabled,
                "hours": r.hours.to_dict() if r.hours else None,
            }
            for r in rows
        ]

    @app.get("/api/candles")
    async def candles(
        instrument: str,
        timeframe: str = "1m",
        price: str = "mid",
        from_: int | None = Query(None, alias="from"),
        to: int | None = None,
        limit: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        tf = _timeframe(timeframe)
        if price not in PRICES:
            raise HTTPException(400, f"price must be one of {list(PRICES)}")
        row = await server.store.instrument(server.source, instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {instrument!r}")
        bars = await server.reader.candles(
            server.source, instrument, tf, price=price, start=from_, end=to, limit=limit
        )
        records = bars.rename(columns={"ts": "t"}).to_dict("records")
        return {
            "instrument": instrument,
            "timeframe": tf.name,
            "price": price,
            "decimals": row.decimals,
            "candles": records,
        }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        client = server.hub.connect()
        sender = asyncio.create_task(_send_loop(ws, client))
        try:
            while True:
                message = await ws.receive_json()
                await _handle(server, client, message)
        except WebSocketDisconnect:
            pass
        finally:
            server.hub.disconnect(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    return app


def _timeframe(name: str):
    try:
        return parse_timeframe(name)
    except ValueError:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}") from None


async def _send_loop(ws: WebSocket, client: Client) -> None:
    while True:
        message = await client.queue.get()
        if message is None:  # client fell too far behind
            await ws.close(code=1013, reason="too slow, reconnect")
            return
        await ws.send_json(message)


async def _handle(server: Server, client: Client, message: Any) -> None:
    if not isinstance(message, dict):
        client.send({"type": "error", "error": "messages must be JSON objects"})
        return
    kind = message.get("type")
    if kind == "ping":
        client.send({"type": "pong", "server_time": server.hub.hello()["server_time"]})
        return
    if kind not in ("subscribe", "unsubscribe"):
        client.send({"type": "error", "error": f"unknown message type {kind!r}"})
        return
    instrument = message.get("instrument")
    price = message.get("price", "mid")
    try:
        tf = parse_timeframe(str(message.get("timeframe", "")))
    except ValueError as exc:
        client.send({"type": "error", "error": str(exc)})
        return
    if price not in PRICES or not isinstance(instrument, str):
        client.send({"type": "error", "error": "subscribe needs instrument and a valid price"})
        return
    if kind == "subscribe":
        if await server.store.instrument(server.source, instrument) is None:
            client.send({"type": "error", "error": f"unknown instrument {instrument!r}"})
            return
        await server.hub.subscribe(client, instrument, tf, price)
        client.send(
            {"type": "subscribed", "instrument": instrument, "timeframe": tf.name, "price": price}
        )
    else:
        server.hub.unsubscribe(client, instrument, tf, price)
        client.send(
            {"type": "unsubscribed", "instrument": instrument, "timeframe": tf.name, "price": price}
        )
