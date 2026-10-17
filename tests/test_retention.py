"""Storage steps 2 (docs/decisions-retention.md): tick retention, M1 depth per instrument with
the daily purge of older minutes, the dry run, `purge` with VACUUM, the access log, rotated
service logs and compose log limits.

Retention deletes data, so the rule every test here comes back to: nothing newer than the
limit is ever deleted. A minute goes only once it ended at or before `now - depth` (`ts + 60
<= cutoff`); a tick day goes only once the whole UTC day ended at or before `now - days`.
Both limits are in UTC seconds, so a change of summer time cannot move them; the grids below
include the US and European changes and clocks on and around midnight UTC.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from openchart import main as cli
from openchart.config import Config
from openchart.market.cache import CandleCache
from openchart.market.instruments import METAL_HOURS
from openchart.market.series import SeriesReader
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.fake import aggregate_candles, synthetic_m1
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database
from openchart.store.ticks import SCHEMA, TickWriter
from tests.conftest import GOLD

try:
    from openchart import retention as rt
except ImportError:  # before the code: the tests fail, none is skipped
    rt = None

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
I = "XAU_USD"  # noqa: E741


def utc(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


# Clocks: around the US changes (8 Mar and 1 Nov 2026, 07:00 and 06:00 UTC), the European
# ones (29 Mar and 25 Oct 2026, 01:00 UTC), on midnight UTC and just either side of it.
NOWS = [
    utc(2026, 3, 8, 6, 59, 59),
    utc(2026, 3, 8, 7, 0, 0),
    utc(2026, 3, 8, 7, 0, 1),
    utc(2026, 3, 29, 1, 0, 0),
    utc(2026, 10, 25, 0, 59, 59, 500000),
    utc(2026, 10, 25, 1, 0, 0),
    utc(2026, 11, 1, 6, 0, 0),
    utc(2026, 11, 1, 5, 59, 59),
    utc(2026, 10, 17, 0, 0, 0),
    utc(2026, 10, 16, 23, 59, 59),
    utc(2026, 10, 16, 23, 59, 59, 999000),
    utc(2026, 10, 17, 0, 0, 0, 1000),
    utc(2026, 10, 17, 0, 0, 59),
    utc(2026, 10, 17, 0, 1, 0),
]


# The choices


def test_the_choices_and_defaults():
    assert list(rt.TICK_RETENTION) == ["7d", "30d", "90d", "1y", "forever", "off"]
    assert [rt.TICK_RETENTION[k] for k in rt.TICK_RETENTION] == [7, 30, 90, 365, None, None]
    assert rt.DEFAULT_TICK_RETENTION == "30d"
    assert list(rt.M1_DEPTHS) == ["3m", "1y", "2y"]
    assert rt.DEFAULT_M1_DEPTH == "2y"
    # Rounded up: never less than promised (3 months is at most 92 days).
    assert [rt.m1_depth_days(k, 2.0) for k in rt.M1_DEPTHS] == [92, 366, 731]
    assert rt.m1_depth_days(None, 2.0) == 731  # the default follows OPENCHART_BACKFILL_YEARS
    assert rt.m1_depth_days(None, 5.0) == 1827
    with pytest.raises(ValueError):
        rt.m1_depth_days("5y", 2.0)


@pytest.mark.parametrize("now", NOWS)
def test_the_cutoff_is_never_later_than_the_limit(now):
    for days in (7, 30, 90, 365, 92, 366, 731):
        cut = rt.cutoff(now, days)
        assert isinstance(cut, int)
        assert cut <= now - days * DAY < cut + 1


# Ticks


def write_day(root: Path, instrument: str, day: str, *, part: bool = False) -> Path:
    folder = root / "fake" / instrument / f"date={day}"
    folder.mkdir(parents=True, exist_ok=True)
    start = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
    table = pa.table(
        {
            "ts": pa.array([start * 1_000_000, (start + DAY - 1) * 1_000_000], SCHEMA[0].type),
            "bid": [1.0, 1.1],
            "ask": [1.2, 1.3],
        },
        schema=SCHEMA,
    )
    pq.write_table(table, folder / ("part-1-000001.parquet" if part else "data.parquet"))
    return folder


def day_end(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() + DAY


def seed_days(root: Path, now: float, back: int = 400) -> list[str]:
    today = datetime.fromtimestamp(now, UTC).date()
    days = [str(today - timedelta(days=k)) for k in range(back)]
    for d in days:
        write_day(root, I, d)
    write_day(root, "EUR_USD", days[-1])
    return days


@pytest.mark.parametrize("now", NOWS)
def test_expired_tick_days_never_include_a_day_that_ends_after_the_limit(tmp_path, now):
    days = seed_days(tmp_path, now)
    for keep in (7, 30, 90, 365):
        cut = now - keep * DAY
        expired = {(d.instrument, d.day) for d in rt.expired_tick_days(tmp_path, "fake", now, keep)}
        for d in days:
            assert ((I, d) in expired) == (day_end(d) <= cut), (keep, d)
        assert ("EUR_USD", days[-1]) in expired


async def test_the_writer_deletes_only_expired_days_and_logs_each(tmp_path, caplog):
    now = utc(2026, 10, 17, 0, 0, 0)
    days = seed_days(tmp_path, now, back=40)
    writer = TickWriter(tmp_path, "fake")
    with caplog.at_level(logging.INFO, logger="openchart.store.ticks"):
        gone = await asyncio.to_thread(writer.delete_expired, now, 30)
    assert sorted(d.day for d in gone if d.instrument == I) == sorted(
        d for d in days if day_end(d) <= now - 30 * DAY
    )
    left = sorted(p.name for p in (tmp_path / "fake" / I).iterdir())
    assert left == sorted(f"date={d}" for d in days if day_end(d) > now - 30 * DAY)
    assert all(d.bytes > 0 for d in gone)
    lines = [r.getMessage() for r in caplog.records if "retention" in r.getMessage()]
    assert len(lines) == len(gone)
    assert all("ticks" in line for line in lines)


def test_a_day_still_being_written_is_never_deleted(tmp_path):
    """Part files mean the day was not compacted: refuse, whatever its date."""
    now = utc(2026, 10, 17, 0, 0, 0)
    write_day(tmp_path, I, "2026-08-01", part=True)
    assert rt.expired_tick_days(tmp_path, "fake", now, 7) == []


# M1 depth


H = METAL_HOURS


@pytest.fixture
async def store(tmp_path):
    async with await Database.open(tmp_path / "t.db") as db:
        s = CandleStore(db.conn)
        await s.upsert_instrument("fake", GOLD, H)
        yield s


@pytest.mark.parametrize("now", NOWS)
async def test_the_m1_purge_deletes_only_minutes_that_ended_before_the_limit(store, now):
    start = int(now) - 9 * DAY
    await store.upsert_m1("fake", I, synthetic_m1(start, int(now) + 120))  # every minute
    before = await store.timestamps("fake", I)
    cut = rt.cutoff(now, 7)
    count, oldest, newest = await store.count_m1_before("fake", I, cut)
    deleted = await store.purge_m1_before("fake", I, cut)
    after = await store.timestamps("fake", I)
    gone = sorted(set(before) - set(after))
    assert deleted == count == len(gone) > 0
    assert (oldest, newest) == (gone[0], gone[-1])
    assert all(ts + 60 <= now - 7 * DAY for ts in gone)
    assert all(ts + 60 > cut for ts in after)  # the first minute kept ends after the limit
    assert after[0] == gone[-1] + 60


async def test_the_m1_purge_bumps_the_epoch_before_and_after(store):
    await store.upsert_m1("fake", I, synthetic_m1(utc(2026, 10, 5), utc(2026, 10, 7)))
    key = ("fake", I)
    seen = []

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("DELETE FROM CANDLES_M1"):
            seen.append(store._epoch.get(key, 0))

    await store.conn.set_trace_callback(trace)
    e0 = store.epoch(*key)
    await store.purge_m1_before("fake", I, int(utc(2026, 10, 6)))
    assert seen and seen[0] > e0
    assert store.epoch(*key) > seen[0]


async def test_after_the_purge_the_derived_bars_equal_a_fresh_computation(store):
    """Deleting old minutes moves the hour where M1 takes over from H1: the cached 4h, D and
    W bars there must be recomputed, as if computed from scratch."""
    now = utc(2026, 10, 14, 15)
    h1 = aggregate_candles(synthetic_m1(int(now) - 60 * DAY, int(now), H, base=2001.0), 3600)
    await store.upsert("fake", I, h1, "H1")
    await store.upsert_m1("fake", I, synthetic_m1(int(now) - 20 * DAY, int(now), H))
    cache = CandleCache(store, clock=lambda: now)
    await cache.extend_all("fake", I)
    await store.purge_m1_before("fake", I, rt.cutoff(now, 10))
    cached = SeriesReader(store, cache=cache)
    plain = SeriesReader(store)
    for name in ("4h", "1D", "1W", "1h"):
        a = await cached.candles("fake", I, TIMEFRAMES[name], limit=5000, now=now)
        b = await plain.candles("fake", I, TIMEFRAMES[name], limit=5000, now=now)
        assert a.equals(b), name


async def test_nothing_older_nothing_to_delete(store):
    await store.upsert_m1("fake", I, synthetic_m1(utc(2026, 10, 10), utc(2026, 10, 11)))
    assert await store.purge_m1_before("fake", I, int(utc(2026, 10, 10))) == 0
    assert await store.count_m1_before("fake", I, int(utc(2026, 10, 10))) == (0, None, None)
    assert await store.purge_m1_before("fake", "NOPE", int(utc(2026, 10, 10))) == 0


async def test_the_depth_is_stored_per_instrument(store):
    assert (await store.instrument("fake", I)).m1_depth is None  # the default
    await store.set_m1_depth("fake", I, "3m")
    assert (await store.instrument("fake", I)).m1_depth == "3m"
    await store.upsert_instrument("fake", GOLD, H)  # re-adding keeps the choice
    assert (await store.instrument("fake", I)).m1_depth == "3m"
    with pytest.raises(ValueError):
        await store.set_m1_depth("fake", I, "10y")


# The server's daily job and the dry run


@pytest.fixture
def seeded(harness):
    """A data folder with ticks of 40 days and minutes either side of the 2-year limit."""
    now = time.time()

    async def seed():
        async with await Database.open(harness.config.db_path) as db:
            s = CandleStore(db.conn)
            await s.upsert_instrument("fake", GOLD, H)
            await s.upsert_m1("fake", I, synthetic_m1(int(now) - 735 * DAY, int(now) - 725 * DAY))
            await s.upsert_m1("fake", I, synthetic_m1(int(now) - 2 * DAY, int(now) - 60))

    asyncio.run(seed())
    days = seed_days(harness.config.ticks_dir, now, back=40)
    return harness, now, days


async def m1_stamps(config: Config) -> list[int]:
    async with await Database.open(config.db_path) as db:
        return await CandleStore(db.conn).timestamps("fake", I)


def test_the_dry_run_shows_what_would_go_and_deletes_nothing(seeded, capsys):
    harness, now, days = seeded
    before = asyncio.run(m1_stamps(harness.config))
    files = sorted(p for p in harness.config.ticks_dir.rglob("*") if p.is_file())
    code = cli.main(["retention", "--data-dir", str(harness.config.data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing was deleted" in out
    assert "XAU_USD" in out and "minutes" in out and "tick" in out
    expired = [d for d in days if day_end(d) <= now - 30 * DAY]
    assert expired and all(d in out for d in (expired[0], expired[-1]))
    assert asyncio.run(m1_stamps(harness.config)) == before
    assert sorted(p for p in harness.config.ticks_dir.rglob("*") if p.is_file()) == files


def test_the_server_job_purges_and_logs(seeded, caplog):
    harness, now, days = seeded
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from tests.conftest import run_setup

    with caplog.at_level(logging.INFO), TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        assert any(t.get_name() == "retention" for t in harness.server._tasks)
        report = c.portal.call(harness.server.run_retention, now)
    stamps = asyncio.run(m1_stamps(harness.config))
    old = [ts for ts in stamps if ts < now - 3 * DAY]
    assert old and min(old) + 60 > now - 731 * DAY  # older ones went, younger ones stayed
    assert max(old) >= now - 726 * DAY
    left = sorted(p.name for p in (harness.config.ticks_dir / "fake" / I).iterdir())
    assert left == sorted(f"date={d}" for d in days if day_end(d) > now - 30 * DAY)
    messages = [r.getMessage() for r in caplog.records]
    assert any("retention" in m and "minutes" in m for m in messages)
    assert report.m1 and report.ticks


@pytest.mark.parametrize("choice", ["forever", "off"])
def test_forever_and_off_delete_no_ticks(seeded, choice):
    harness, now, _ = seeded
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from tests.conftest import run_setup

    files = sorted(p for p in harness.config.ticks_dir.rglob("*") if p.is_file())
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        r = c.put("/api/settings", json={"tick_retention": choice})
        assert r.status_code == 200, r.text
        assert r.json()["tick_retention"] == choice
        assert r.json()["store_ticks"] is (choice != "off")
        c.portal.call(harness.server.run_retention, now)
    assert sorted(p for p in harness.config.ticks_dir.rglob("*") if p.is_file()) == files


def test_retention_runs_once_per_utc_day():
    day = rt.utc_day
    assert rt.retention_due(None, utc(2026, 10, 16, 23, 59, 59))
    last = day(utc(2026, 10, 16, 12))
    assert not rt.retention_due(last, utc(2026, 10, 16, 23, 59, 59))
    assert rt.retention_due(last, utc(2026, 10, 17, 0, 0, 0))
    assert not rt.retention_due(day(utc(2026, 10, 17)), utc(2026, 10, 17, 0, 0, 30))


# Settings and instruments through the API


def test_settings_tick_retention(client):
    s = client.get("/api/settings").json()
    assert s["tick_retention"] == "30d" and s["store_ticks"] is True
    assert s["tick_retention_choices"] == list(rt.TICK_RETENTION)
    assert client.put("/api/settings", json={"tick_retention": "2d"}).status_code == 422
    r = client.put("/api/settings", json={"store_ticks": False}).json()  # an older page
    assert (r["tick_retention"], r["store_ticks"]) == ("off", False)
    r = client.put("/api/settings", json={"store_ticks": True}).json()
    assert (r["tick_retention"], r["store_ticks"]) == ("30d", True)
    r = client.put("/api/settings", json={"tick_retention": "1y"}).json()
    assert (r["tick_retention"], r["store_ticks"]) == ("1y", True)


def test_m1_depth_through_the_api(client):
    rows = {r["instrument"]: r for r in client.get("/api/instruments").json()}
    assert rows[I]["m1_depth"] == "2y" and rows[I]["m1_depth_default"] is True
    assert client.put(f"/api/instruments/{I}/m1-depth", json={"m1_depth": "6m"}).status_code == 422
    assert client.put("/api/instruments/NOPE/m1-depth", json={"m1_depth": "1y"}).status_code == 404
    r = client.put(f"/api/instruments/{I}/m1-depth", json={"m1_depth": "3m"})
    assert r.status_code == 200, r.text
    rows = {r["instrument"]: r for r in client.get("/api/instruments").json()}
    assert rows[I]["m1_depth"] == "3m" and rows[I]["m1_depth_default"] is False


async def test_a_longer_depth_downloads_the_missing_minutes_again(store):
    """Its download was finished; a longer depth clears the mark so the feed resumes it."""
    await store.mark_download_complete("fake", I, "M1", int(utc(2026, 7, 1)))
    await store.set_m1_depth("fake", I, "3m")
    assert await rt.change_m1_depth(store, "fake", I, "3m", now=utc(2026, 10, 1)) is False
    assert (await store.download_marks("fake", I))[1] == int(utc(2026, 7, 1))
    assert await rt.change_m1_depth(store, "fake", I, "1y", now=utc(2026, 10, 1)) is True
    assert (await store.download_marks("fake", I))[1] is None
    assert (await store.instrument("fake", I)).m1_depth == "1y"


def test_the_feed_downloads_back_to_the_instruments_depth():
    from openchart.market.feed import Feed
    from openchart.providers.fake import FakeProvider

    feed = Feed(FakeProvider(), None, [I, "EUR_USD"], m1_depth_days={I: 92})
    now = utc(2026, 10, 14, 15)
    assert feed.m1_target(I, now) == rt.cutoff(now, 92)
    assert feed.m1_target("EUR_USD", now) == rt.cutoff(now, 731)


# purge with VACUUM


async def _fill(path: Path) -> None:
    async with await Database.open(path) as db:
        s = CandleStore(db.conn)
        await s.upsert_instrument("fake", GOLD, H)
        await s.upsert_m1("fake", I, synthetic_m1(int(utc(2026, 1, 1)), int(utc(2026, 3, 1))))


@pytest.mark.parametrize("vacuum", [True, False])
def test_purge_runs_vacuum_unless_told_not_to(tmp_path, capsys, vacuum):
    config = replace(Config(), data_dir=tmp_path)
    asyncio.run(_fill(config.db_path))
    size = config.db_path.stat().st_size
    args = ["purge", I, "--source", "fake", "--yes", "--data-dir", str(tmp_path)]
    assert cli.main(args if vacuum else [*args, "--no-vacuum"]) == 0
    out = capsys.readouterr().out
    after = config.db_path.stat().st_size
    if vacuum:
        assert after < size / 2
        assert "VACUUM" in out and "MB" in out
    else:
        assert after >= size * 0.9
        assert "--no-vacuum" in out or "not compacted" in out


# Logs


def test_the_access_log_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("OPENCHART_ACCESS_LOG", raising=False)
    assert cli.access_log_enabled() is False
    monkeypatch.setenv("OPENCHART_ACCESS_LOG", "1")
    assert cli.access_log_enabled() is True
    monkeypatch.setenv("OPENCHART_ACCESS_LOG", "0")
    assert cli.access_log_enabled() is False


def test_run_passes_the_access_log_switch_to_uvicorn(monkeypatch):
    import uvicorn

    seen = {}

    class Stop(Exception):
        pass

    def fake_config(app, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.delenv("OPENCHART_ACCESS_LOG", raising=False)
    with pytest.raises(Stop):
        cli.serve_app(object(), "127.0.0.1", 8000, "info")
    assert seen["access_log"] is False


def test_only_the_newest_five_rotated_service_logs_are_kept(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "service.log").write_text("live")
    (logs / "other.txt").write_text("not ours")
    rotated = [logs / f"service-20261001T0{k}0000.000.log" for k in range(8)]
    for k, p in enumerate(rotated):
        p.write_text("x" * 10)
        stamp = utc(2026, 10, 1) + k * 3600
        import os

        os.utime(p, (stamp, stamp))
    gone = rt.prune_service_logs(logs)
    assert sorted(gone) == sorted(rotated[:3])
    assert sorted(p.name for p in logs.iterdir()) == sorted(
        ["service.log", "other.txt", *(p.name for p in rotated[3:])]
    )
    assert rt.prune_service_logs(logs) == []
    assert rt.prune_service_logs(tmp_path / "missing") == []


def test_compose_limits_the_container_logs():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        logging_ = service.get("logging")
        assert logging_ == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }, name


# The migration (synthetic databases only)


async def test_migration_adds_the_depth_column_and_keeps_rows(tmp_path):
    import aiosqlite

    from openchart.store.migrate import discover_migrations, migrate

    path = tmp_path / "old.db"
    all_ = discover_migrations()
    new = [m for m in all_ if m.name.endswith("m1_depth")]
    assert len(new) == 1
    async with aiosqlite.connect(path) as conn:
        await migrate(conn, [m for m in all_ if m.version < new[0].version])
        await conn.execute(
            "INSERT INTO instruments (instrument, source, provider_symbol, display_name,"
            " decimals, pip_size, enabled) VALUES ('XAU_USD', 'oanda', 'XAU_USD', 'Gold', 3,"
            " 0.01, 1)"
        )
        await conn.commit()
        assert await migrate(conn, all_) == [m.version for m in all_ if m.version >= new[0].version]
        async with conn.execute("SELECT instrument, m1_depth FROM instruments") as cur:
            assert await cur.fetchall() == [("XAU_USD", None)]
