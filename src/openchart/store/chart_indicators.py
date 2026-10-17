"""Indicators per chart (CI-3, decision D1). A chart is (source, instrument); its list is
in legend order and replaced whole. Every write moves the chart's revision on by one.

The store keeps what it is given; checking parameters against definitions is the API's
job (`api/chart_indicators.py`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

KINDS = ("builtin", "custom")


@dataclass
class Instance:
    uid: str
    kind: str  # builtin | custom
    indicator: str  # the definition's id
    params: dict[str, Any] = field(default_factory=dict)
    visible: bool = True
    price_labels: bool = True
    legend_values: bool = True

    def to_json(self) -> dict[str, Any]:
        """The shape a layout keeps and a browser sends."""
        return {
            "uid": self.uid,
            "kind": self.kind,
            "id": self.indicator,
            "params": self.params,
            "visible": self.visible,
            "price_labels": self.price_labels,
            "legend_values": self.legend_values,
        }


class ChartIndicatorStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def revision(self, source: str, instrument: str) -> int:
        """0 for a chart whose list was never set."""
        async with self.conn.execute(
            "SELECT revision FROM chart_indicator_revisions WHERE source=? AND instrument=?",
            (source, instrument),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list(self, source: str, instrument: str) -> list[Instance]:
        sql = (
            "SELECT uid, kind, indicator, params, visible, price_labels, legend_values"
            " FROM chart_indicators WHERE source=? AND instrument=? ORDER BY position"
        )
        async with self.conn.execute(sql, (source, instrument)) as cur:
            rows = await cur.fetchall()
        return [
            Instance(r[0], r[1], r[2], json.loads(r[3]), bool(r[4]), bool(r[5]), bool(r[6]))
            for r in rows
        ]

    async def replace(self, source: str, instrument: str, instances: list[Instance]) -> int:
        """The chart's whole list; returns its new revision. The caller has checked the
        list (unique uids, known kinds), so no statement here can fail on it: a rollback
        would take other writers' uncommitted rows on this shared connection with it."""
        revision = await self.revision(source, instrument) + 1
        await self.conn.execute(
            "DELETE FROM chart_indicators WHERE source=? AND instrument=?", (source, instrument)
        )
        await self.conn.executemany(
            "INSERT INTO chart_indicators (source, instrument, position, uid, kind, indicator,"
            " params, visible, price_labels, legend_values) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    source,
                    instrument,
                    position,
                    i.uid,
                    i.kind,
                    i.indicator,
                    json.dumps(i.params),
                    int(i.visible),
                    int(i.price_labels),
                    int(i.legend_values),
                )
                for position, i in enumerate(instances)
            ],
        )
        await self.conn.execute(
            "INSERT INTO chart_indicator_revisions (source, instrument, revision, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (source, instrument)"
            " DO UPDATE SET revision=excluded.revision, updated_at=excluded.updated_at",
            (source, instrument, revision, int(time.time())),
        )
        await self.conn.commit()
        return revision

    async def charts_using(self, kind: str, indicator: str) -> list[tuple[str, str, int]]:
        """(source, instrument, instances) of every chart with this definition on it."""
        sql = (
            "SELECT source, instrument, COUNT(*) FROM chart_indicators"
            " WHERE kind=? AND indicator=? GROUP BY source, instrument ORDER BY source, instrument"
        )
        async with self.conn.execute(sql, (kind, indicator)) as cur:
            return [(r[0], r[1], int(r[2])) for r in await cur.fetchall()]
