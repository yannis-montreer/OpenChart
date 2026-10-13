"""Command line entry point: `openchart <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openchart import __version__
from openchart.config import Config, ConfigError, load_config
from openchart.market.backfill import backfill, deep_fill
from openchart.market.cache import CandleCache
from openchart.market.gaps import find_gaps
from openchart.market.instruments import default_hours
from openchart.providers.base import AuthError, Provider, ProviderError
from openchart.providers.registry import create_provider
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


@asynccontextmanager
async def _context(config: Config, need_provider: bool = True) -> AsyncIterator[Context]:
    # Build the provider first: if it fails, no database thread is left open
    # (an unclosed aiosqlite connection keeps the process alive).
    provider = create_provider(config) if need_provider else None
    try:
        db = await Database.open(config.db_path)
    except BaseException:
        if provider is not None:
            await provider.aclose()
        raise
    try:
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
    provider = create_provider(config)
    try:
        result = await provider.test_connection()
    finally:
        await provider.aclose()
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
    await ctx.store.upsert_instrument(ctx.provider.name, info, default_hours(instrument, info.kind))


async def _cmd_backfill(config: Config, args: argparse.Namespace) -> int:
    years = args.years if args.years is not None else config.backfill_years
    start = datetime.now(UTC) - timedelta(days=365.25 * years)
    async with _context(config) as ctx:
        await _ensure_instrument(ctx, args.instrument)
        before = await ctx.store.count(ctx.provider.name, args.instrument)
        print(f"Backfilling {args.instrument} M1 from {start:%Y-%m-%d} ({before} rows stored)")

        def progress(written: int, ts: int, direction: str) -> None:
            where = "up to" if direction == "forward" else "searched back to"
            print(f"\r  {written:>9,} candles written, {where} {_fmt(ts)}   ", end="", flush=True)

        result = await backfill(ctx.provider, ctx.store, args.instrument, start, progress=progress)
        after = await ctx.store.count(ctx.provider.name, args.instrument)
        print(
            f"\nDone: reached the latest complete candle. {after - before:,} new rows "
            f"({result.written:,} written incl. updates), {after:,} total, "
            f"{_fmt(result.first_ts)} to {_fmt(result.last_ts)}"
        )
        if args.deep:
            print(f"Deep H1 history for {args.instrument}, as far back as the broker has it")
            deep = await deep_fill(ctx.provider, ctx.store, args.instrument, progress=progress)
            print(f"\n  H1 from {_fmt(deep.first_ts)} to {_fmt(deep.last_ts)}")
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


async def _cmd_run(config: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from openchart.api.app import create_app
    from openchart.server import Server

    host = args.host or config.host
    port = args.port or config.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "listening on %s: there is no login until M4, so only do this on a private "
            "network such as Tailscale",
            host,
        )
    if args.data_dir:
        config = replace(config, data_dir=Path(args.data_dir))
    app = create_app(Server(config))
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=config.log_level.lower())
    )
    await server.serve()
    return 0


COMMANDS = {
    "migrate": _cmd_migrate,
    "test-connection": _cmd_test_connection,
    "backfill": _cmd_backfill,
    "stats": _cmd_stats,
    "gaps": _cmd_gaps,
    "rebuild-cache": _cmd_rebuild_cache,
    "run": _cmd_run,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openchart", description="OpenChart server")
    parser.add_argument("--version", action="version", version=f"openchart {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the database")
    sub.add_parser("test-connection", help="check the broker credentials")
    p = sub.add_parser("backfill", help="download M1 history (idempotent, resumable)")
    p.add_argument("instrument", help="for example XAU_USD")
    p.add_argument("--years", type=float, help="history depth (default from config: 2)")
    p.add_argument(
        "--deep", action="store_true", help="also download H1 history as far back as available"
    )
    sub.add_parser("stats", help="row counts and coverage per instrument")
    p = sub.add_parser("rebuild-cache", help="recompute the cached 4h/D/W bars")
    p.add_argument("instrument", nargs="?", help="default: all instruments")
    p = sub.add_parser("gaps", help="list holes in stored history")
    p.add_argument("instrument")
    p.add_argument("--source", default="oanda")
    p.add_argument("--min-minutes", type=int, default=10)
    p = sub.add_parser("run", help="start the server: API, websocket and live feed")
    p.add_argument("--host", help="bind address (default 127.0.0.1, from config)")
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
