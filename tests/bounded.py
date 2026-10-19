"""`portal.call` with a deadline.

The TestClient's portal waits for the server's event loop for as long as it takes. A call
that never returns then holds the whole run until the CI job's limit; this one fails after
`timeout` seconds with the call's name (see docs/decisions-ci-test-timeout.md).
"""

from __future__ import annotations

import inspect
from typing import Any

import anyio

TIMEOUT = 30.0


def call(client, fn, *args: Any, timeout: float = TIMEOUT) -> Any:
    """`client.portal.call(fn, *args)`, or AssertionError after `timeout` seconds."""

    async def bounded():
        with anyio.fail_after(timeout):
            result = fn(*args)
            if inspect.isawaitable(result):
                result = await result
            return result

    try:
        return client.portal.call(bounded)
    except TimeoutError:
        name = getattr(fn, "__qualname__", repr(fn))
        raise AssertionError(f"{name} did not finish within {timeout:.0f}s") from None
