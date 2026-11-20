"""The feed's answer to the broker rejecting the credentials (docs/decisions-feed-rejections.md).

OANDA answers 401/403 "Insufficient authorization to perform request" for a revoked or
wrong token, and also, as on 15 November 2026, while OANDA itself is down with the token
still valid. Its answer does not tell the two apart, so a rejection is first taken as
possibly passing: the feed retries after `retry_delay` (30 s doubling to 10 min, minus up
to a fifth of jitter) for `RETRY_LIMIT` (6 hours), then stops as it always did. Only a
rejection that is certain (`AuthError.certain`: a token that cannot be one) stops at once.

The server (`Server._run_feed`) holds one `Rejections` while it lasts, for the status and
for the two Telegram messages (down, then recovered or stopped).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

RETRY_FIRST = 30.0  # seconds before the first retry
RETRY_MAX = 600.0  # the delay doubles up to this
RETRY_LIMIT = 6 * 3600.0  # retrying ends this long after the first rejection
JITTER = 0.2  # each delay is shortened by up to this fraction, at random


def retry_delay(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Seconds to wait before retry number `attempt` (0 for the first): 30, 60, 120, 240,
    480, then 600, each shortened by up to a fifth (several servers never retry in step,
    and none waits more than 10 minutes)."""
    base = min(RETRY_MAX, RETRY_FIRST * 2 ** min(attempt, 16))
    return base * (1 - JITTER * rand())


@dataclass
class Rejections:
    """One stretch of rejections, from the first to the recovery or the stop."""

    since: float  # server time of the first rejection
    error: str  # the broker's words, as before ("OANDA rejected the credentials: ...")
    attempts: int = 0  # retries scheduled so far
    next_attempt: float | None = None  # None while an attempt is running

    @property
    def gives_up_at(self) -> float:
        return self.since + RETRY_LIMIT

    def expired(self, now: float) -> bool:
        return now - self.since >= RETRY_LIMIT

    def to_json(self, now: float) -> dict[str, Any]:
        return {
            "reason": "credentials_rejected",
            "since": self.since,
            "attempts": self.attempts,
            "next_attempt": self.next_attempt,
            "next_attempt_in": (
                None if self.next_attempt is None else max(0.0, round(self.next_attempt - now))
            ),
            "gives_up_at": self.gives_up_at,
        }
