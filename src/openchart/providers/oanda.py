"""OANDA v20 provider (REST candles, pricing stream)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from openchart.config import OandaConfig
from openchart.providers.base import (
    OHLC,
    AuthError,
    Candle,
    Heartbeat,
    InstrumentInfo,
    ProviderError,
    RequestError,
    StreamEvent,
    TestResult,
    Tick,
)

log = logging.getLogger(__name__)

MAX_CANDLES_PER_REQUEST = 5000


def _ts(value: str) -> float:
    # With "Accept-Datetime-Format: UNIX" OANDA sends times as "1700000000.000000000".
    return float(value)


def _ohlc(raw: dict[str, str]) -> OHLC:
    return OHLC(float(raw["o"]), float(raw["h"]), float(raw["l"]), float(raw["c"]))


def parse_candle(raw: dict[str, Any]) -> Candle:
    return Candle(
        ts=int(_ts(raw["time"])),
        bid=_ohlc(raw["bid"]),
        ask=_ohlc(raw["ask"]),
        mid=_ohlc(raw["mid"]),
        volume=int(raw.get("volume", 0)),
        complete=bool(raw.get("complete", False)),
    )


def parse_stream_line(line: str) -> StreamEvent | None:
    """Parse one line of the pricing stream. Returns None for lines to ignore."""
    if not line.strip():
        return None
    msg = json.loads(line)
    kind = msg.get("type")
    if kind == "HEARTBEAT":
        return Heartbeat(ts=_ts(msg["time"]))
    if kind == "PRICE":
        bids, asks = msg.get("bids") or [], msg.get("asks") or []
        if not bids or not asks:
            return None
        return Tick(
            instrument=msg["instrument"],
            ts=_ts(msg["time"]),
            bid=float(bids[0]["price"]),
            ask=float(asks[0]["price"]),
        )
    return None


def _error_text(resp: httpx.Response) -> str:
    """OANDA's `errorMessage`, else the start of the body. Never headers (the token)."""
    try:
        text = str(resp.json().get("errorMessage") or resp.text)
    except ValueError:
        text = resp.text[:300]
    if text.strip():
        return text
    # OANDA's API answers its own errors in JSON; an empty answer comes from in front of
    # it, typically a request it could not read at all (a malformed Authorization header).
    return "empty response body (the request was refused before OANDA's API read it)"


def _request_text(path: str, params: dict[str, Any] | None) -> str:
    """The request as safe to log: path and query parameters only."""
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    return f"{path}?{query}" if query else path


def token_problem(token: str) -> str | None:
    """Why a token cannot be one, without showing it. A token is printable text without
    spaces; a paste that went wrong (Ctrl+V typed as a character, a line break) is not."""
    if not token:
        return "the OANDA token is empty"
    if any(not ch.isprintable() or ch.isspace() for ch in token):
        return (
            f"the OANDA token ({len(token)} characters) contains spaces or control "
            "characters: a paste that went wrong? Enter it again"
        )
    return None


class OandaProvider:
    name = "oanda"

    def __init__(
        self,
        config: OandaConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        stream_transport: httpx.AsyncBaseTransport | None = None,
        request_delay: float = 0.1,
        max_retries: int = 5,
        stream_read_timeout: float = 30.0,
    ):
        if not config.configured:
            raise AuthError("OANDA token and account ID are not configured")
        self.config = config
        self.token_problem = token_problem(config.token)
        self.request_delay = request_delay
        self.max_retries = max_retries
        headers = {
            "Authorization": f"Bearer {config.token}",
            "Accept-Datetime-Format": "UNIX",
            "User-Agent": "OpenChart",
        }
        self._rest = httpx.AsyncClient(
            base_url=config.rest_url,
            headers=headers,
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )
        # The read timeout is a backstop; heartbeat supervision lives in providers.stream.
        self._stream = httpx.AsyncClient(
            base_url=config.stream_url,
            headers=headers,
            timeout=httpx.Timeout(10.0, read=stream_read_timeout),
            transport=stream_transport or transport,
        )

    async def aclose(self) -> None:
        await self._rest.aclose()
        await self._stream.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.token_problem:  # it would only be refused, without saying why
            raise AuthError(self.token_problem)
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._rest.get(path, params=params)
            except httpx.TransportError as exc:
                err: str = f"network error: {exc!r}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise AuthError(f"OANDA rejected the credentials: {_error_text(resp)}")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise RequestError(
                        f"OANDA HTTP {resp.status_code} for {_request_text(path, params)}: "
                        f"{_error_text(resp)}"
                    )
                err = f"HTTP {resp.status_code}"
            if attempt == self.max_retries:
                raise ProviderError(f"OANDA request failed after retries: {err}")
            log.warning("OANDA %s failed (%s), retrying in %.1fs", path, err, delay)
            await asyncio.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 30.0)
        raise AssertionError("unreachable")

    async def list_instruments(self) -> list[InstrumentInfo]:
        data = await self._get(f"/v3/accounts/{self.config.account_id}/instruments")
        out = []
        for raw in data.get("instruments", []):
            out.append(
                InstrumentInfo(
                    instrument=raw["name"],
                    provider_symbol=raw["name"],
                    display_name=raw.get("displayName", raw["name"]),
                    decimals=int(raw.get("displayPrecision", 5)),
                    pip_size=10.0 ** int(raw.get("pipLocation", -4)),
                    kind=raw.get("type", ""),
                )
            )
        return out

    async def fetch_candles(
        self, instrument: str, start: datetime, end: datetime, granularity: str = "M1"
    ) -> AsyncIterator[Candle]:
        start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
        cursor, include_first = start_ts, True
        while cursor < end_ts and cursor < datetime.now(UTC).timestamp():
            params: dict[str, Any] = {
                "granularity": granularity,
                "price": "MBA",
                "from": cursor,
                "count": MAX_CANDLES_PER_REQUEST,
            }
            if not include_first:
                params["includeFirst"] = "false"
            data = await self._get(f"/v3/instruments/{instrument}/candles", params)
            batch = [parse_candle(raw) for raw in data.get("candles", [])]
            # A short page does NOT mean we reached the present: with includeFirst=false
            # OANDA counts the skipped candle toward `count`, so full pages hold 4999.
            # The only end signals are an empty page or the still-forming candle.
            if not batch:
                return
            for candle in batch:
                if candle.ts >= end_ts:
                    return
                if not candle.complete:
                    return  # the current, still-forming candle: we are at the present
                if candle.ts >= start_ts:
                    yield candle
            if batch[-1].ts <= cursor and not include_first:
                raise ProviderError(f"OANDA pagination stalled at {cursor} for {instrument}")
            cursor, include_first = batch[-1].ts, False
            if self.request_delay:
                await asyncio.sleep(self.request_delay)

    async def fetch_page_before(
        self, instrument: str, end: datetime | None, granularity: str = "M1"
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "granularity": granularity,
            "price": "MBA",
            "count": MAX_CANDLES_PER_REQUEST,
        }
        end_ts = None if end is None else int(end.timestamp())
        if end_ts is not None:
            params["to"] = end_ts  # without `to`: the latest candles, up to the forming one
        data = await self._get(f"/v3/instruments/{instrument}/candles", params)
        if self.request_delay:
            await asyncio.sleep(self.request_delay)
        # The candle at `to` itself may come back and count toward `count` (as the
        # skipped first candle does going forward), and the newest page ends with the
        # still-forming candle: both are dropped, so a full page can hold 4999. Only an
        # empty result means there is nothing older.
        batch = (parse_candle(raw) for raw in data.get("candles", []))
        return [c for c in batch if c.complete and (end_ts is None or c.ts < end_ts)]

    async def stream_ticks(self, instruments: list[str]) -> AsyncIterator[StreamEvent]:
        path = f"/v3/accounts/{self.config.account_id}/pricing/stream"
        params = {"instruments": ",".join(instruments)}
        if self.token_problem:
            raise AuthError(self.token_problem)
        async with self._stream.stream("GET", path, params=params) as resp:
            if resp.status_code in (401, 403):
                await resp.aread()
                raise AuthError(f"OANDA rejected the credentials: {_error_text(resp)}")
            if resp.status_code != 200:
                await resp.aread()
                raise ProviderError(f"OANDA stream HTTP {resp.status_code}: {_error_text(resp)}")
            async for line in resp.aiter_lines():
                event = parse_stream_line(line)
                if event is not None:
                    yield event

    async def test_connection(self) -> TestResult:
        try:
            data = await self._get(f"/v3/accounts/{self.config.account_id}/summary")
        except ProviderError as exc:
            return TestResult(False, str(exc))
        account = data.get("account", {})
        return TestResult(
            True,
            f"Connected to OANDA {self.config.environment} account {account.get('id', '?')} "
            f"({account.get('currency', '?')})",
        )
