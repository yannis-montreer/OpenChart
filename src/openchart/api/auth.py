"""Single-password authentication (SPEC section 8).

- The password is stored as an argon2 hash in the settings table.
- A login creates a random session token. The browser gets it in an HTTP-only,
  SameSite=Lax cookie valid for 30 days; the database stores only its SHA-256 hash.
- Failed logins are rate limited per client address and globally.
- Before setup, a request that does not come from this computer must present a
  one-time setup code printed in the server log, so nobody else on the network can
  claim a fresh server.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import defaultdict, deque

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from openchart.settings import SettingsStore

SESSION_COOKIE = "openchart_session"
SESSION_SECONDS = 30 * 86400
MIN_PASSWORD_LENGTH = 8
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_hasher = PasswordHasher()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PasswordError(ValueError):
    pass


def check_new_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")


class Auth:
    def __init__(self, conn: aiosqlite.Connection, settings: SettingsStore):
        self.conn = conn
        self.settings = settings
        self.limiter = LoginLimiter()
        self.setup_code = secrets.token_hex(3).upper()  # 6 characters, shown in the log

    # Password

    async def password_set(self) -> bool:
        return bool(await self.settings.get("password_hash"))

    async def set_password(self, password: str) -> None:
        check_new_password(password)
        digest = await asyncio.to_thread(_hasher.hash, password)
        await self.settings.set("password_hash", digest)
        await self.delete_all_sessions()

    async def verify_password(self, password: str) -> bool:
        digest = await self.settings.get("password_hash")
        if not digest or not isinstance(password, str):
            return False
        try:
            await asyncio.to_thread(_hasher.verify, digest, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if _hasher.check_needs_rehash(digest):
            await self.settings.set(
                "password_hash", await asyncio.to_thread(_hasher.hash, password)
            )
        return True

    # Sessions

    async def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), now, now + SESSION_SECONDS),
        )
        await self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        await self.conn.commit()
        return token

    async def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        async with self.conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash=?", (_hash_token(token),)
        ) as cur:
            row = await cur.fetchone()
        return bool(row) and row[0] > time.time()

    async def delete_session(self, token: str | None) -> None:
        if token:
            await self.conn.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),)
            )
            await self.conn.commit()

    async def delete_all_sessions(self) -> None:
        await self.conn.execute("DELETE FROM sessions")
        await self.conn.commit()

    # Setup gate

    def setup_allowed_from(self, client_host: str | None, code: str | None) -> bool:
        if client_host in LOOPBACK:
            return True
        return bool(code) and secrets.compare_digest(code.strip().upper(), self.setup_code)


class LoginLimiter:
    """At most `per_client` failures per client and `overall` failures in total within
    `window` seconds; further attempts wait until the oldest failure expires."""

    def __init__(
        self, per_client: int = 5, overall: int = 30, window: float = 900.0, clock=time.monotonic
    ):
        self.per_client = per_client
        self.overall = overall
        self.window = window
        self.clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._all: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, client: str) -> float:
        """Seconds to wait before `client` may try again (0 = allowed now)."""
        now = self.clock()
        mine = self._failures[client]
        self._prune(mine, now)
        self._prune(self._all, now)
        waits = [0.0]
        if len(mine) >= self.per_client:
            waits.append(mine[0] + self.window - now)
        if len(self._all) >= self.overall:
            waits.append(self._all[0] + self.window - now)
        return max(waits)

    def failed(self, client: str) -> None:
        now = self.clock()
        self._failures[client].append(now)
        self._all.append(now)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)
