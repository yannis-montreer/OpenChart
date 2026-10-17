"""What is kept, and for how long (storage steps 2, docs/decisions-retention.md).

- **Raw ticks:** 7, 30 (default), 90 days, a year, forever, or not stored at all ("off").
  A UTC day's folder goes once the whole day ended at or before `now - days`.
- **Minutes (M1) per instrument:** 3 months, 1 year or 2 years (default: 2 years, or
  `OPENCHART_BACKFILL_YEARS`). A minute goes once it ended at or before `now - depth`.
  1h and longer charts are built from H1 there, which is never deleted.
- **Rotated service logs:** the newest five are kept.

Every limit is in UTC seconds, so a change of summer time cannot move it. The server applies
them at start and once a UTC day (`Server.run_retention`), from a day after the first start
with this code; `openchart retention` shows what that would delete, and deletes nothing.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DAY = 86400
TICK_RETENTION: dict[str, int | None] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "1y": 365,
    "forever": None,
    "off": None,  # no ticks are stored; those already stored are left alone
}
DEFAULT_TICK_RETENTION = "30d"
M1_DEPTHS: dict[str, float] = {"3m": 0.25, "1y": 1.0, "2y": 2.0}  # in years
DEFAULT_M1_DEPTH = "2y"
SERVICE_LOGS_KEPT = 5
# The first run on an install deletes nothing for a day: time to choose other limits first.
SINCE_KEY = "retention_since"
GRACE_SECONDS = DAY


def years_to_days(years: float) -> int:
    """Rounded up: a depth is never shorter than promised."""
    return math.ceil(years * 365.25)


def m1_depth_days(choice: str | None, default_years: float) -> int:
    """Days of minutes kept for an instrument's choice (None: the default)."""
    if choice is None:
        return years_to_days(default_years)
    if choice not in M1_DEPTHS:
        raise ValueError(f"minute history must be one of {list(M1_DEPTHS)}, got {choice!r}")
    return years_to_days(M1_DEPTHS[choice])


def m1_depth_choice(choice: str | None, default_years: float) -> str:
    """The choice to show for an instrument: its own, or the one matching the default."""
    if choice is not None:
        return choice
    days = years_to_days(default_years)
    return next((k for k, y in M1_DEPTHS.items() if years_to_days(y) == days), DEFAULT_M1_DEPTH)


def cutoff(now: float, days: int) -> int:
    """The limit in whole seconds, never after `now - days` (data ending by then goes)."""
    return math.floor(now) - days * DAY


def utc_day(now: float) -> date:
    return datetime.fromtimestamp(now, UTC).date()


def retention_due(last: date | None, now: float) -> bool:
    """At start, and once the UTC date has changed since the last run."""
    return last is None or utc_day(now) > last


# Ticks


@dataclass(frozen=True)
class TickDay:
    instrument: str
    day: str
    path: Path
    files: int
    bytes: int


def _day_end(day: str) -> int | None:
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int((start + timedelta(days=1)).timestamp())


def expired_tick_days(root: str | Path, source: str, now: float, days: int) -> list[TickDay]:
    """Day folders of `source` whose whole UTC day ended at or before `now - days`. A folder
    with part files (a day not compacted yet) or an unreadable name is never listed."""
    limit = cutoff(now, days)
    out = []
    for folder in sorted(Path(root).glob(f"{source}/*/date=*")):
        end = _day_end(folder.name.removeprefix("date="))
        if end is None or end > limit or not folder.is_dir():
            continue
        files = [p for p in folder.iterdir() if p.is_file()]
        if any(p.name.startswith("part-") or p.suffix == ".tmp" for p in files):
            continue
        out.append(
            TickDay(
                folder.parent.name,
                folder.name.removeprefix("date="),
                folder,
                len(files),
                sum(p.stat().st_size for p in files),
            )
        )
    return out


# Minutes


@dataclass(frozen=True)
class M1Purge:
    instrument: str
    depth: str
    days: int
    cutoff: int
    minutes: int
    oldest: int | None
    newest: int | None


async def plan_m1(store, source: str, rows, now: float, default_years: float) -> list[M1Purge]:
    """Per instrument, the minutes its depth would delete (`rows`: `InstrumentRow`s)."""
    out = []
    for row in rows:
        days = m1_depth_days(row.m1_depth, default_years)
        limit = cutoff(now, days)
        n, oldest, newest = await store.count_m1_before(source, row.instrument, limit)
        choice = m1_depth_choice(row.m1_depth, default_years)
        out.append(M1Purge(row.instrument, choice, days, limit, n, oldest, newest))
    return out


async def change_m1_depth(store, source: str, instrument: str, choice: str, now: float) -> bool:
    """Save an instrument's depth. True when it reaches further back than the finished M1
    download did: its mark is cleared so the feed downloads the missing minutes, newest
    first. A shorter depth deletes nothing here: the next daily run does."""
    await store.set_m1_depth(source, instrument, choice)
    _, m1_from = await store.download_marks(source, instrument)
    if m1_from is None:
        return False  # not finished: the download reads the new target when it resumes
    if cutoff(now, m1_depth_days(choice, 0)) >= m1_from:
        return False
    await store.mark_download_complete(source, instrument, "M1", None)
    return True


# Logs


def prune_service_logs(log_dir: str | Path, keep: int = SERVICE_LOGS_KEPT) -> list[Path]:
    """Delete rotated `service-*.log` files beyond the newest `keep` (NSSM renames a full
    log with a timestamp and never deletes old ones). `service.log` itself is never touched."""
    folder = Path(log_dir)
    if not folder.is_dir():
        return []
    rotated = [p for p in folder.glob("service-*.log") if p.is_file()]
    rotated.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    gone = []
    for p in rotated[keep:]:
        try:
            p.unlink()
        except OSError as exc:  # in use or locked: next time
            log.warning("retention: could not delete the old log %s: %s", p.name, exc)
            continue
        gone.append(p)
    if gone:
        log.info("retention: %d old service log(s) deleted, %d kept", len(gone), keep)
    return gone


@dataclass
class Report:
    """What one run deleted (or, for the dry run, would delete)."""

    ticks: list[TickDay] = field(default_factory=list)
    m1: list[M1Purge] = field(default_factory=list)
    logs: list[Path] = field(default_factory=list)


def fmt_ts(ts: int | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def describe(report: Report, tick_choice: str) -> list[str]:
    """The dry run's lines."""
    lines = [f"Raw ticks: kept {tick_choice} ({', '.join(TICK_RETENTION)})."]
    if report.ticks:
        size = sum(d.bytes for d in report.ticks)
        lines.append(f"  {len(report.ticks)} tick day(s) would go, {_mb(size)}:")
        for d in report.ticks:
            lines.append(f"    {d.instrument} {d.day}: {d.files} file(s), {_mb(d.bytes)}")
    else:
        lines.append("  no tick day would go")
    lines.append("Minute history (M1), per instrument:")
    for p in report.m1:
        head = f"  {p.instrument}: {p.depth} ({p.days} days), limit {fmt_ts(p.cutoff)} UTC:"
        if p.minutes:
            lines.append(
                f"{head} {p.minutes:,} minutes would go ({fmt_ts(p.oldest)} to {fmt_ts(p.newest)})"
            )
        else:
            lines.append(f"{head} nothing older")
    return lines
