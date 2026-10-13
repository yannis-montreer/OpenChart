"""Waiting for websocket messages in tests, with a deadline.

`ws.receive_json()` blocks until a message comes. A test that waits for one the server
never sends then hangs, or reads the periodic status messages until some count runs out,
which hides the reason. These helpers fail after `timeout` seconds and say what came.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anyio

TIMEOUT = 10.0


def receive(ws, timeout: float = TIMEOUT) -> dict[str, Any]:
    """The next message, or AssertionError after `timeout` seconds."""

    async def next_message():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    try:
        message = ws.portal.call(next_message)
    except TimeoutError:
        raise AssertionError(f"no websocket message within {timeout:.0f}s") from None
    ws._raise_on_close(message)
    return json.loads(message["text"])


def receive_until(ws, kind: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Skip other messages (status, ticks) until one of type `kind`, within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while (left := deadline - time.monotonic()) > 0:
        try:
            message = receive(ws, left)
        except AssertionError:
            break
        if message["type"] == kind:
            return message
        seen.append(message["type"])
    raise AssertionError(f"no {kind} message within {timeout:.0f}s; got {seen}")
