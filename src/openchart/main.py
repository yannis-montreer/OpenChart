"""Command line entry point: `openchart <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openchart import __version__
from openchart.config import Config, ConfigError, load_config
from openchart.lock import in_use
from openchart.market.backfill import (
    CLOSURE_MINUTES,
    deep_fill,
    download_backward,
    gap_fill,
    repair,
)
from openchart.market.cache import CandleCache
from openchart.market.gaps import find_gaps
from openchart.market.instruments import instrument_hours
from openchart.providers.base import AuthError, Provider, ProviderError
from openchart.providers.registry import create_provider
from openchart.settings import (
    SecretStoreUnavailable,
    SettingsStore,
    effective_config,
    load_app_settings,
    open_secrets,
)
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

log = logging.getLogger("openchart")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request URL at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _fmt(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


@dataclass
class Context:
    config: Config
    store: CandleStore
    provider: Provider


async def _saved_config(config: Config, db: Database) -> Config:
    """Settings and secrets saved through the web UI, over the environment config."""
    settings = SettingsStore(db.conn)
    try:
        secrets = open_secrets(config, await settings.instance_id())
    except SecretStoreUnavailable as exc:
        log.warning("secrets: %s", exc)
        secrets = None
    return effective_config(config, await load_app_settings(settings, config), secrets)


@asynccontextmanager
async def _context(config: Config, need_provider: bool = True) -> AsyncIterator[Context]:
    db = await Database.open(config.db_path)
    provider = None
    try:
        # Any failure here must still close the database: an open aiosqlite
        # connection keeps the process alive.
        config = await _saved_config(config, db)
        if need_provider:
            provider = create_provider(config)
        yield Context(config, CandleStore(db.conn), provider)  # type: ignore[arg-type]
    finally:
        if provider is not None:
            await provider.aclose()
        await db.close()


async def _cmd_migrate(config: Config, args: argparse.Namespace) -> int:
    async with await Database.open(config.db_path):
        pass
    print(f"Database ready at {config.db_path}")
    return 0


async def _cmd_test_connection(config: Config, args: argparse.Namespace) -> int:
    async with _context(config) as ctx:
        result = await ctx.provider.test_connection()
    print(("OK: " if result.ok else "FAILED: ") + result.message)
    return 0 if result.ok else 1


async def _ensure_instrument(ctx: Context, instrument: str) -> None:
    known = {i.instrument: i for i in await ctx.provider.list_instruments()}
    if instrument not in known:
        raise ConfigError(
            f"{instrument} is not available on this {ctx.provider.name} account "
            "(OANDA names use an underscore, e.g. XAU_USD)."
        )
    info = known[instrument]
    await ctx.store.upsert_instrument(ctx.provider.name, info, instrument_hours(info))


async def _cmd_backfill(config: Config, args: argparse.Namespace) -> int:
    """Newest first, like the server: H1 (with --deep), then M1 back to `--years`."""
    years = args.years if args.years is not None else config.backfill_years
    start = datetime.now(UTC) - timedelta(days=365.25 * years)
    async with _context(config) as ctx:
        await _ensure_instrument(ctx, args.instrument)
        before = await ctx.store.count(ctx.provider.name, args.instrument)

        def progress(written: int, ts: int, direction: str) -> None:
            where = "up to" if direction == "forward" else "back to"
            print(f"\r  {written:>9,} candles written, {where} {_fmt(ts)}   ", end="", flush=True)

        if args.deep:
            print(f"Deep H1 history for {args.instrument}, newest first, as far as available")
            deep = await deep_fill(ctx.provider, ctx.store, args.instrument, progress=progress)
            print(f"\n  H1 from {_fmt(deep.oldest)}")
        print(f"M1 for {args.instrument}, newest first, back to {start:%Y-%m-%d} ({before} stored)")
        await gap_fill(ctx.provider, ctx.store, args.instrument)
        result = await download_backward(
            ctx.provider, ctx.store, args.instrument, int(start.timestamp()), progress=progress
        )
        after = await ctx.store.count(ctx.provider.name, args.instrument)
        last = await ctx.store.last_ts(ctx.provider.name, args.instrument)
        print(
            f"\nDone: {after - before:,} new rows, {after:,} total, "
            f"{_fmt(result.oldest)} to {_fmt(last)}"
        )
        if args.deep:
            print("Building the 4h/D/W cache")
            n = await CandleCache(ctx.store).extend_all(ctx.provider.name, args.instrument)
            print(f"  {n:,} bars cached")
    return 0


async def _cmd_rebuild_cache(config: Config, args: argparse.Namespace) -> int:
    async with _context(config, need_provider=False) as ctx:
        cache = CandleCache(ctx.store)
        rows = await ctx.store.instruments()
        for row in rows:
            if args.instrument and row.instrument != args.instrument:
                continue
            n = await cache.rebuild(row.source, row.instrument)
            print(f"{row.source}/{row.instrument}: {n:,} 4h/D/W bars rebuilt")
    return 0


async def _cmd_stats(config: Config, args: argparse.Namespace) -> int:
    async with _context(config, need_provider=False) as ctx:
        rows = await ctx.store.instruments()
        if not rows:
            print("No instruments yet. Run `openchart backfill <INSTRUMENT>` first.")
            return 0
        for row in rows:
            n = await ctx.store.count(row.source, row.instrument)
            first = await ctx.store.first_ts(row.source, row.instrument)
            last = await ctx.store.last_ts(row.source, row.instrument)
            line = f"{row.source}/{row.instrument}: {n:,} M1 candles, {_fmt(first)} to {_fmt(last)}"
            if first and last and row.hours:
                # Expected trading minutes in the stored range, sampled hourly.
                open_hours = sum(1 for t in range(first, last, 3600) if row.hours.is_open(t))
                if open_hours:
                    line += f", {n / (open_hours * 60):.1%} of expected trading minutes"
            print(line)
            h1 = await ctx.store.count(row.source, row.instrument, "H1")
            if h1:
                h1_first = await ctx.store.first_ts(row.source, row.instrument, "H1")
                h1_last = await ctx.store.last_ts(row.source, row.instrument, "H1")
                print(f"  {h1:,} H1 candles (deep history), {_fmt(h1_first)} to {_fmt(h1_last)}")
            for name in ("4h", "1D", "1W"):
                cached = await ctx.store.cache_count(row.source, row.instrument, name)
                if cached:
                    print(f"  {cached:,} cached {name} bars")
            h1_from, m1_from = await ctx.store.download_marks(row.source, row.instrument)
            if h1_from is None or m1_from is None:
                waiting = " and ".join(
                    g for g, mark in (("H1", h1_from), ("M1", m1_from)) if mark is None
                )
                print(f"  download not finished ({waiting}): it continues when the server runs")
    return 0


async def _cmd_purge(config: Config, args: argparse.Namespace) -> int:
    """Delete one instrument and its stored prices, to download it again from scratch."""
    if in_use(config.data_dir):
        print(
            f"An OpenChart server is running on {config.data_dir}: stop it first "
            "(it would keep the instrument in memory and download it again).",
            file=sys.stderr,
        )
        return 1
    name, source = args.instrument.strip().upper(), args.source
    async with _context(config, need_provider=False) as ctx:
        conn = ctx.store.conn
        if await ctx.store.instrument(source, name) is None:
            print(f"No instrument {source}/{name} in {config.db_path}.")
            return 1

        async def count(sql: str) -> int:
            async with conn.execute(sql, (name,)) as cur:
                row = await cur.fetchone()
            return row[0] if row else 0

        m1 = await ctx.store.count(source, name)
        h1 = await ctx.store.count(source, name, "H1")
        drawings = await count("SELECT COUNT(*) FROM drawings WHERE instrument=?")
        alerts = await count("SELECT COUNT(*) FROM alerts WHERE instrument=?")
        ticks = config.ticks_dir / source / name
        print(f"{source}/{name}: {m1:,} M1 and {h1:,} H1 candles, cached bars, closures")
        print(f"  tick files: {ticks if ticks.exists() else 'none'}")
        keep = (
            "deleted too"
            if args.drawings_and_alerts
            else "kept (--drawings-and-alerts deletes them)"
        )
        print(f"  {drawings} drawing(s) and {alerts} alert(s): {keep}")
        if not args.yes:
            print("Nothing deleted. Run again with --yes to delete.")
            return 0
        await ctx.store.purge_instrument(source, name)
        if args.drawings_and_alerts:
            await conn.execute(
                "DELETE FROM alert_events WHERE alert_id IN"
                " (SELECT id FROM alerts WHERE instrument=?)",
                (name,),
            )
            await conn.execute("DELETE FROM alerts WHERE instrument=?", (name,))
            await conn.execute("DELETE FROM drawings WHERE instrument=?", (name,))
            await conn.commit()
        if ticks.exists():
            shutil.rmtree(ticks)
        print(f"Deleted. Add {name} again in Settings, Instruments to download it afresh.")
    return 0


async def _cmd_gaps(config: Config, args: argparse.Namespace) -> int:
    async with _context(config, need_provider=False) as ctx:
        source = args.source
        row = await ctx.store.instrument(source, args.instrument)
        if row is None:
            print(f"No stored data for {source}/{args.instrument}.")
            return 1
        stamps = await ctx.store.timestamps(source, args.instrument)
        gaps = find_gaps(stamps, row.hours, args.min_minutes)
        for gap in gaps:
            print(f"  {_fmt(gap.before)} -> {_fmt(gap.after)}: {gap.open_minutes} open minutes")
        print(
            f"{len(gaps)} gap(s) of at least {args.min_minutes} minutes during expected "
            f"trading hours in {len(stamps):,} candles. Holidays and broker outages show up "
            "here too."
        )
    return 0


async def _cmd_repair(config: Config, args: argparse.Namespace) -> int:
    async with _context(config) as ctx:
        source = ctx.provider.name
        row = await ctx.store.instrument(source, args.instrument)
        if row is None:
            print(f"No stored data for {source}/{args.instrument}.")
            return 1

        def progress(done: int, total: int) -> None:
            print(f"\r  {done}/{total} requests", end="", flush=True)

        print(f"Scanning {source}/{args.instrument} M1 for holes inside the stored range")
        result = await repair(
            ctx.provider,
            ctx.store,
            args.instrument,
            row.hours,
            min_open_minutes=args.min_minutes,
            progress=progress,
        )
        if result.requests:
            print()
        stamps = await ctx.store.timestamps(source, args.instrument)
        left = {g.before: g for g in find_gaps(stamps, row.hours, args.min_minutes)}
        for gap in result.holes:
            now = left.get(gap.before)
            if now is None:
                state = "filled"
            elif now.after == gap.after:
                state = "nothing at the broker (holiday, early close or outage)"
            else:
                state = f"partly filled, the broker has nothing until {_fmt(now.after)}"
            span = f"{_fmt(gap.before)} -> {_fmt(gap.after)}"
            print(f"  {span}: {gap.open_minutes} open minutes, {state}")
        print(
            f"{len(result.holes)} hole(s) of at least {args.min_minutes} open minutes, "
            f"{result.filled:,} candles filled with {result.requests} request(s)."
        )
        if args.min_minutes <= CLOSURE_MINUTES and row.hours is not None:
            await ctx.store.mark_closures_scanned(source, args.instrument, int(time.time()))
            closures = await ctx.store.closures(source, args.instrument)
            print(
                f"{len(closures)} market closure(s) of {CLOSURE_MINUTES}+ minutes recorded; "
                "drawn lines and alerts skip them."
            )
        if result.filled:
            n = await CandleCache(ctx.store).extend_all(source, args.instrument)
            print(f"4h/D/W cache brought up to date ({n:,} bars recomputed).")
    return 0


async def _cmd_run(config: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from openchart.api.app import create_app
    from openchart.server import Server

    if args.data_dir:
        config = replace(config, data_dir=Path(args.data_dir))
    async with await Database.open(config.db_path) as db:
        app_settings = await load_app_settings(SettingsStore(db.conn), config)
    # --host, then OPENCHART_HOST, then the "other devices" setting from the web UI.
    host = (
        args.host
        or os.environ.get("OPENCHART_HOST")
        or ("0.0.0.0" if app_settings.listen_on_network else "127.0.0.1")
    )
    port = args.port or config.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "listening on %s:%s for other devices. Keep it on a private network such as "
            "Tailscale; never forward this port on your router.",
            host,
            port,
        )
    app = create_app(Server(config))
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=config.log_level.lower())
    )
    await server.serve()
    return 0


async def _cmd_reset_password(config: Config, args: argparse.Namespace) -> int:
    """Recovery from the server's own console: set a new password, log everyone out."""
    import getpass

    from openchart.api.auth import Auth, PasswordError

    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != first:
        print("The two passwords differ.", file=sys.stderr)
        return 1
    async with await Database.open(config.db_path) as db:
        try:
            await Auth(db.conn, SettingsStore(db.conn)).set_password(first)
        except PasswordError as exc:
            print(exc, file=sys.stderr)
            return 1
    print("Password changed. Every browser has been logged out.")
    return 0


COMMANDS = {
    "migrate": _cmd_migrate,
    "test-connection": _cmd_test_connection,
    "backfill": _cmd_backfill,
    "stats": _cmd_stats,
    "gaps": _cmd_gaps,
    "repair": _cmd_repair,
    "rebuild-cache": _cmd_rebuild_cache,
    "reset-password": _cmd_reset_password,
    "purge": _cmd_purge,
    "run": _cmd_run,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openchart", description="OpenChart server")
    parser.add_argument("--version", action="version", version=f"openchart {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the database")
    sub.add_parser("test-connection", help="check the broker credentials")
    p = sub.add_parser("backfill", help="download history, newest first (idempotent, resumable)")
    p.add_argument("instrument", help="for example XAU_USD")
    p.add_argument("--years", type=float, help="history depth (default from config: 2)")
    p.add_argument(
        "--deep", action="store_true", help="also download H1 history as far back as available"
    )
    sub.add_parser("stats", help="row counts and coverage per instrument")
    sub.add_parser("reset-password", help="set a new web password (if you forgot it)")
    p = sub.add_parser("rebuild-cache", help="recompute the cached 4h/D/W bars")
    p.add_argument("instrument", nargs="?", help="default: all instruments")
    p = sub.add_parser("gaps", help="list holes in stored history")
    p.add_argument("instrument")
    p.add_argument("--source", default="oanda")
    p.add_argument("--min-minutes", type=int, default=10)
    p = sub.add_parser("repair", help="refill holes inside stored M1 history from the broker")
    p.add_argument("instrument")
    p.add_argument(
        "--min-minutes",
        type=int,
        default=5,
        help="smallest hole to refetch, in open minutes (default 5; 1 checks every minute)",
    )
    p = sub.add_parser("purge", help="delete an instrument and its stored prices (server stopped)")
    p.add_argument("instrument")
    p.add_argument("--source", default="oanda")
    p.add_argument("--yes", action="store_true", help="really delete (default: only show)")
    p.add_argument(
        "--drawings-and-alerts",
        action="store_true",
        help="also delete the instrument's drawings, alerts and alert log",
    )
    p = sub.add_parser("run", help="start the server: API, websocket and live feed")
    p.add_argument(
        "--host", help="bind address (default: 127.0.0.1, or 0.0.0.0 if enabled in Settings)"
    )
    p.add_argument("--port", type=int, help="port (default 8000, from config)")
    p.add_argument("--data-dir", help="database and tick folder (default data/, from config)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(config.log_level)
    try:
        return asyncio.run(COMMANDS[args.command](config, args))
    except KeyboardInterrupt:
        return 130
    except (ConfigError, AuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
