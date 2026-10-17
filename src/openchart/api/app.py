"""FastAPI application: REST routes and the /ws websocket.

Every /api route except health, setup status, setup and login needs a session cookie;
so does the websocket. Requests that change state, and websocket connections, must come
from a page served by this server (Origin check), which blocks cross-site requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from openchart import API_VERSION, __version__
from openchart.api.auth import SESSION_COOKIE
from openchart.api.chart_indicators import ChartIndicators
from openchart.api.hub import Client
from openchart.api.routes_account import account_routes, client_host
from openchart.api.routes_alerts import alert_routes
from openchart.api.routes_charts import chart_routes
from openchart.api.routes_custom_indicators import custom_routes
from openchart.api.routes_definitions import definition_routes
from openchart.api.routes_drawings import drawing_routes
from openchart.api.routes_indicators import indicator_routes
from openchart.api.routes_layouts import layout_routes
from openchart.market import sessions
from openchart.market.aggregate import PRICES
from openchart.market.timeframes import TIMEFRAMES, parse_timeframe
from openchart.server import Server

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Reachable without a session (SPEC section 8).
PUBLIC = {"/api/health", "/api/setup/status", "/api/setup", "/api/login"}
# Reachable without a session only before setup, by someone allowed to run it.
SETUP_ONLY = {"/api/setup/defaults", "/api/settings/test/oanda", "/api/settings/test/telegram"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class WebFiles(StaticFiles):
    """The web UI. Built files under assets/ have a content hash in their name and never
    change; index.html is checked on every load, so a rebuild takes effect at once
    (a cached page would keep running the previous build)."""

    async def __call__(self, scope, receive, send):
        # Only GET and HEAD read files. Anything else that reached the mount matched no
        # route, so it is "not found" (the base class says 405), as without a built UI.
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = scope["path"].startswith("/assets/")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


def encodable(value: Any) -> Any:
    """`value` with every float that JSON cannot carry (NaN, infinities) as its text."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: encodable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [encodable(v) for v in value]
    return value


def same_origin(origin: str | None, host: str | None) -> bool:
    """No Origin header (curl, scripts) is fine; a browser page from elsewhere is not."""
    return not origin or urlsplit(origin).netloc == host


_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>OpenChart</title>
<body style="font-family:system-ui;background:#0f1218;color:#d6dae3;padding:2rem">
<h1>OpenChart server is running</h1>
<p>The web interface is not built. In <code>frontend/</code> run <code>npm ci</code> and
<code>npm run build</code>, then reload. The API is at <a href="/docs">/docs</a>.</p>"""


def create_app(server: Server) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await server.start()
        try:
            yield
        finally:
            await server.stop()

    app = FastAPI(title="OpenChart", version=__version__, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's own 422, except that a NaN or an infinity in the echoed input (Python's
        JSON reader accepts them) is sent as text: as a number it cannot be encoded, and
        the request became a 500."""
        return JSONResponse({"detail": encodable(jsonable_encoder(exc.errors()))}, 422)

    app.state.server = server

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if request.method not in SAFE_METHODS and not same_origin(
                request.headers.get("origin"), request.headers.get("host")
            ):
                return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
            if path not in PUBLIC and not await server.auth.session_valid(
                request.cookies.get(SESSION_COOKIE)
            ):
                setup_ok = (
                    path in SETUP_ONLY
                    and not await server.auth.password_set()
                    and server.auth.setup_allowed_from(
                        client_host(request), request.headers.get("x-setup-code")
                    )
                )
                if not setup_ok:
                    return JSONResponse({"detail": "Login required."}, status_code=401)
        return await call_next(request)

    app.include_router(account_routes(server))
    app.include_router(drawing_routes(server))
    app.include_router(alert_routes(server))
    charts = ChartIndicators(server)  # one lock for every write to charts' indicators
    app.include_router(indicator_routes(server))
    app.include_router(chart_routes(server, charts))
    app.include_router(custom_routes(server, charts))
    app.include_router(definition_routes(server, charts))
    app.include_router(layout_routes(server, charts))

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/api/version")
    async def version() -> dict[str, Any]:
        return {"version": __version__, "api_version": API_VERSION}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return server.status()

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        """Timeframes and the trading-day anchor, so clients never hardcode them."""
        return {
            "api_version": API_VERSION,
            "server_time": time.time(),
            "prices": list(PRICES),
            "timeframes": [
                {"name": tf.name, "seconds": tf.seconds, "kind": tf.kind.value}
                for tf in TIMEFRAMES.values()
            ],
            "session": {
                "timezone": sessions.DAY_ANCHOR_TZ,
                "day_anchor_hour": sessions.DAY_ANCHOR_HOUR,
                "week_anchor_weekday": sessions.WEEK_ANCHOR_WEEKDAY,
            },
        }

    @app.get("/api/instruments")
    async def instruments() -> list[dict[str, Any]]:
        rows = await server.store.instruments(server.source)
        closures = {r.instrument: await server.store.closures(r.source, r.instrument) for r in rows}
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
                "closures": [list(c) for c in closures[r.instrument]],
            }
            for r in rows
        ]

    @app.get("/api/candles")
    async def candles(
        instrument: str,
        timeframe: str = "1m",
        price: str = "mid",
        # Times as UTC epoch seconds, bounded so SQLite can take them (beyond 64 bits it
        # cannot: a 500 before); 2**53 is the largest a browser holds exactly.
        from_: int | None = Query(None, alias="from", ge=-(2**53), le=2**53),
        to: int | None = Query(None, ge=-(2**53), le=2**53),
        limit: int | None = Query(None, ge=1, le=2**31),
        forming: bool = True,
    ) -> dict[str, Any]:
        tf = _timeframe(timeframe)
        if price not in PRICES:
            raise HTTPException(400, f"price must be one of {list(PRICES)}")
        row = await server.store.instrument(server.source, instrument)
        if row is None:
            raise HTTPException(404, f"unknown instrument {instrument!r}")
        bars = await server.reader.candles(
            server.source,
            instrument,
            tf,
            price=price,
            start=from_,
            end=to,
            limit=limit,
            forming=forming,
        )
        records = bars.rename(columns={"ts": "t"}).to_dict("records")
        if "forming" in bars.attrs:  # the bar that holds the live minute: in memory, not stored
            records[-1]["forming"] = True
        return {
            "instrument": instrument,
            "timeframe": tf.name,
            "price": price,
            "decimals": row.decimals,
            "candles": records,
        }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        allowed = same_origin(ws.headers.get("origin"), ws.headers.get("host"))
        if not allowed or not await server.auth.session_valid(ws.cookies.get(SESSION_COOKIE)):
            await ws.accept()
            await ws.close(code=4401, reason="login required")
            return
        # Join the broadcasts before the browser sees the socket open: it reloads drawings
        # and alerts when it does, so a change made after that must reach this client.
        client = server.hub.connect()
        try:
            await ws.accept()
        except Exception:
            server.hub.disconnect(client)
            raise
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

    # The built web UI, served last so /api and /ws take precedence.
    if (WEB_DIR / "index.html").is_file():
        app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")
    else:

        @app.get("/", include_in_schema=False)
        async def not_built() -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT)

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
