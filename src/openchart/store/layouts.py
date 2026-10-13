"""Saved chart layouts (SPEC section 11). The layout itself is a JSON object the API
validates; the store keeps it as text. The default layout's id is a setting."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

DEFAULT_KEY = "default_layout"


@dataclass
class Layout:
    id: str
    name: str
    state: dict[str, Any]
    updated_at: int

    def summary(self, default_id: str | None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "updated_at": self.updated_at,
            "default": self.id == default_id,
        }

    def to_json(self, default_id: str | None) -> dict[str, Any]:
        return {**self.summary(default_id), "layout": self.state}


class LayoutStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def list(self) -> list[Layout]:
        sql = "SELECT id, name, json, updated_at FROM layouts ORDER BY name COLLATE NOCASE, id"
        async with self.conn.execute(sql) as cur:
            return [Layout(r[0], r[1], json.loads(r[2]), r[3]) for r in await cur.fetchall()]

    async def get(self, layout_id: str) -> Layout | None:
        sql = "SELECT id, name, json, updated_at FROM layouts WHERE id=?"
        async with self.conn.execute(sql, (layout_id,)) as cur:
            r = await cur.fetchone()
        return Layout(r[0], r[1], json.loads(r[2]), r[3]) if r else None

    async def create(self, name: str, state: dict[str, Any]) -> Layout:
        layout = Layout(secrets.token_hex(8), name, state, int(time.time()))
        await self.conn.execute(
            "INSERT INTO layouts (id, name, json, updated_at) VALUES (?, ?, ?, ?)",
            (layout.id, layout.name, json.dumps(state), layout.updated_at),
        )
        await self.conn.commit()
        return layout

    async def save(self, layout: Layout) -> Layout:
        layout.updated_at = int(time.time())
        await self.conn.execute(
            "UPDATE layouts SET name=?, json=?, updated_at=? WHERE id=?",
            (layout.name, json.dumps(layout.state), layout.updated_at, layout.id),
        )
        await self.conn.commit()
        return layout

    async def delete(self, layout_id: str) -> Layout | None:
        layout = await self.get(layout_id)
        if layout is not None:
            await self.conn.execute("DELETE FROM layouts WHERE id=?", (layout_id,))
            await self.conn.commit()
        return layout
