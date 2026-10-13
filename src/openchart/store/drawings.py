"""Drawings (SPEC section 10). The server stores geometry and style only.

A drawing has anchor points `{t, p}`: `t` is a UTC epoch second (clock time, not a bar
index, so the line drawn and the line an alert checks are the same) and `p` a price.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

TYPES = {"hline": 1, "trendline": 2, "ray": 2, "extended": 2}  # type -> number of points
DASHES = ("solid", "dashed", "dotted")
DEFAULT_STYLE = {"color": "#4d8dff", "width": 2, "dash": "solid"}


@dataclass
class Drawing:
    id: str
    instrument: str
    type: str
    points: list[dict[str, float]]
    style: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_STYLE))
    label: str | None = None
    created_at: int = 0
    updated_at: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instrument": self.instrument,
            "type": self.type,
            "points": self.points,
            "style": self.style,
            "label": self.label,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row(r: tuple) -> Drawing:
    return Drawing(
        id=r[0],
        instrument=r[1],
        type=r[2],
        points=json.loads(r[3]),
        style={**DEFAULT_STYLE, **json.loads(r[4] or "{}")},
        label=r[5],
        created_at=r[6],
        updated_at=r[7],
    )


_COLUMNS = "id, instrument, type, points_json, style_json, label, created_at, updated_at"


class DrawingStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def list(self, instrument: str | None = None) -> list[Drawing]:
        sql = (
            f"SELECT {_COLUMNS} FROM drawings"
            " WHERE (? IS NULL OR instrument=?) ORDER BY created_at, id"
        )
        async with self.conn.execute(sql, (instrument, instrument)) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def get(self, drawing_id: str) -> Drawing | None:
        async with self.conn.execute(
            f"SELECT {_COLUMNS} FROM drawings WHERE id=?", (drawing_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def create(
        self,
        instrument: str,
        type_: str,
        points: list[dict[str, float]],
        style: dict[str, Any],
        label: str | None = None,
    ) -> Drawing:
        now = int(time.time())
        drawing = Drawing(
            secrets.token_hex(8),
            instrument,
            type_,
            points,
            {**DEFAULT_STYLE, **style},
            label,
            now,
            now,
        )
        await self.conn.execute(
            f"INSERT INTO drawings ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                drawing.id,
                instrument,
                type_,
                json.dumps(points),
                json.dumps(drawing.style),
                label,
                now,
                now,
            ),
        )
        await self.conn.commit()
        return drawing

    async def update(
        self,
        drawing_id: str,
        *,
        points: list[dict[str, float]] | None = None,
        style: dict[str, Any] | None = None,
        label: str | None = None,
        clear_label: bool = False,
    ) -> Drawing | None:
        drawing = await self.get(drawing_id)
        if drawing is None:
            return None
        if points is not None:
            drawing.points = points
        if style is not None:
            drawing.style = {**drawing.style, **style}
        if clear_label:
            drawing.label = None
        elif label is not None:
            drawing.label = label
        drawing.updated_at = int(time.time())
        await self.conn.execute(
            "UPDATE drawings SET points_json=?, style_json=?, label=?, updated_at=? WHERE id=?",
            (
                json.dumps(drawing.points),
                json.dumps(drawing.style),
                drawing.label,
                drawing.updated_at,
                drawing_id,
            ),
        )
        await self.conn.commit()
        return drawing

    async def delete(self, drawing_id: str) -> Drawing | None:
        drawing = await self.get(drawing_id)
        if drawing is not None:
            await self.conn.execute("DELETE FROM drawings WHERE id=?", (drawing_id,))
            await self.conn.commit()
        return drawing

    async def delete_all(self, instrument: str) -> list[Drawing]:
        drawings = await self.list(instrument)
        await self.conn.execute("DELETE FROM drawings WHERE instrument=?", (instrument,))
        await self.conn.commit()
        return drawings
