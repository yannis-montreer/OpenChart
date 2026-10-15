"""Command line entry point: `openchart <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openchart import __version__
from openchart.config import Config, ConfigError, load_config
from openchart.lock import DataFolderInUse, DataFolderLock, exclusive
from openchart.market.backfill import (
    CLOSURE_MINUTES,
    deep_fill,
    download_backward,
    gap_fill,
    repair,
)
from openchart.market.cache import CandleCache
from openchart.market.gaps import coverage, find_gaps
from openchart.market.instruments import instrument_hours
from openchart.providers.base import AuthError, Provider, ProviderError
from openchart.providers.registry import create_provider
from openchart.settings import (
    SecretKeyChanged,
    SecretStoreUnavailable,
    SettingsStore,
    effective_config,
    load_app_settings,
    open_secrets,
)
from openchart.store.candles import CandleStore
from openchart.store.migrate import UpgradeNeeded
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


EXIT_DATA_IN_USE = 4  # a server (or another command that writes) holds the data folder


@asynccontextmanager
async def _database(config: Config, *, writes: bool, what: str) -> AsyncIterator[Database]:
    """The data folder's database for one command.

    A command that writes holds the folder's lock throughout, so it refuses while a server
    runs (and a server refuses to start meanwhile). A command that only reads runs beside
    a server, but opening the database can upgrade it: that happens only while nobody
    else holds the folder; with a server running, a pending upgrade makes it refuse.
    `what` names the command for the message ("run openchart stats").
    """
    if writes:
        with exclusive(config.data_dir, what):
            async with await Database.open(config.db_path) as db:
                yield db
        return
    lock = DataFolderLock(config.data_dir)
    if lock.acquire():
        try:
            db = await Database.open(config.db_path)  # may upgrade: nobody else uses it
        finally:
            lock.release()
    else:
        try:
            db = await Database.open(config.db_path, upgrade=False)
        except UpgradeNeeded as exc:
            raise DataFolderInUse(
                f"{str(exc).capitalize()}, and an OpenChart server is running on "
                f"{config.data_dir}: stop it, run `openchart migrate`, then {what} again. "
                "Nothing was changed."
            ) from None
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def _context(
    config: Config, need_provider: bool = True, *, writes: bool, what: str
) -> AsyncIterator[Context]:
    async with (
        _database(config, writes=writes, what=what) as db,
        _provider_context(config, db, need_provider) as ctx,
    ):
        yield ctx


@asynccontextmanager
async def _provider_context(
    config: Config, db: Database, need_provider: bool
) -> AsyncIterator[Context]:
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


async def _cmd_migrate(config: Config, args: argparse.Namespace) -> int:
    async with _database(config, writes=True, what="run openchart migrate"):
        pass
    print(f"Database ready at {config.db_path}")
    return 0


async def _cmd_test_connection(config: Config, args: argparse.Namespace) -> int:
    async with _context(config, writes=False, what="test the connection") as ctx:
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
    async with _context(config, writes=True, what="run openchart backfill") as ctx:
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
    async with _context(
        config, need_provider=False, writes=True, what="run openchart rebuild-cache"
    ) as ctx:
        cache = CandleCache(ctx.store)
        rows = await ctx.store.instruments()
        for row in rows:
            if args.instrument and row.instrument != args.instrument:
                continue
            n = await cache.rebuild(row.source, row.instrument)
            print(f"{row.source}/{row.instrument}: {n:,} 4h/D/W bars rebuilt")
    return 0


async def _cmd_stats(config: Config, args: argparse.Namespace) -> int:
    config = _data_dir(config, args)
    async with _context(
        config, need_provider=False, writes=False, what="run openchart stats"
    ) as ctx:
        rows = await ctx.store.instruments()
        if not rows:
            print("No instruments yet. Run `openchart backfill <INSTRUMENT>` first.")
        for row in rows:
            n = await ctx.store.count(row.source, row.instrument)
            first = await ctx.store.first_ts(row.source, row.instrument)
            last = await ctx.store.last_ts(row.source, row.instrument)
            line = f"{row.source}/{row.instrument}: {n:,} M1 candles, {_fmt(first)} to {_fmt(last)}"
            if first and last and row.hours:
                cov = coverage(
                    await ctx.store.timestamps(row.source, row.instrument),
                    row.hours,
                    await ctx.store.closures(row.source, row.instrument),
                )
                if cov.expected:
                    line += f", {cov.ratio:.1%} of expected trading minutes"
                if cov.outside:
                    line += f" (and {cov.outside:,} outside the trading hours)"
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

        async def count(sql: str) -> int:
            async with ctx.store.conn.execute(sql) as cur:
                return (await cur.fetchone())[0]

        # To compare two installs (for example after moving the data folder).
        drawings = await count("SELECT COUNT(*) FROM drawings")
        alerts = await count("SELECT COUNT(*) FROM alerts")
        active = await count("SELECT COUNT(*) FROM alerts WHERE status = 'active'")
        layouts = await count("SELECT COUNT(*) FROM layouts")
        print(f"Drawings: {drawings}, alerts: {alerts} ({active} active), layouts: {layouts}")
    return 0


async def _cmd_purge(config: Config, args: argparse.Namespace) -> int:
    """Delete one instrument and its stored prices, to download it again from scratch."""
    config = _data_dir(config, args)
    name, source = args.instrument.strip().upper(), args.source
    async with _context(
        config, need_provider=False, writes=True, what="run openchart purge"
    ) as ctx:
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
    async with _context(
        config, need_provider=False, writes=False, what="run openchart gaps"
    ) as ctx:
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
    async with _context(config, writes=True, what="run openchart repair") as ctx:
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


def in_container() -> bool:
    """Docker creates /.dockerenv; Podman sets `container`."""
    return Path("/.dockerenv").exists() or bool(os.environ.get("container"))  # noqa: SIM112 (lowercase by design)


async def _cmd_run(config: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from openchart.api.app import create_app
    from openchart.server import Server

    if args.data_dir:
        config = replace(config, data_dir=Path(args.data_dir))
    # Refuse before anything else if another server (or a command) holds the folder; the
    # server then holds the lock itself for as long as it runs.
    async with _database(config, writes=True, what="start the server") as db:
        settings = SettingsStore(db.conn)
        app_settings = await load_app_settings(settings, config)
        instance_id = await settings.instance_id()
    try:  # before opening the port: a key that cannot open secrets.enc stops the server
        secrets = open_secrets(config, instance_id)
        secrets.get("oanda_token")
    except SecretKeyChanged as exc:
        log.error("%s", exc)
        print(_KEY_CHANGED.format(error=exc, data_dir=config.data_dir), file=sys.stderr)
        return EXIT_KEY_CHANGED
    except SecretStoreUnavailable:
        pass  # the server starts without stored tokens and shows why (Settings, setup)
    # --host, then OPENCHART_HOST, then the "other devices" setting from the web UI.
    host = (
        args.host
        or os.environ.get("OPENCHART_HOST")
        or ("0.0.0.0" if app_settings.listen_on_network else "127.0.0.1")
    )
    port = args.port or config.port
    if in_container():
        # 0.0.0.0 inside a container says nothing about who can connect: the published
        # port does (docker-compose.yml: 127.0.0.1 unless OPENCHART_BIND says otherwise).
        log.info(
            "listening on %s:%s inside the container; who can connect depends on the "
            "published port (docker-compose.yml: OPENCHART_BIND, 127.0.0.1 by default)",
            host,
            port,
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
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
    if not server.started:  # uvicorn logged why; a service manager must see a failure
        return EXIT_STARTUP_FAILED
    return 0


EXIT_KEY_CHANGED = 2
EXIT_STARTUP_FAILED = 3  # uvicorn's own code for it
_KEY_CHANGED = """
OpenChart did not start: {error}.

The broker and Telegram tokens in {data_dir}/secrets.enc were encrypted with
another key. Without them there would be no live prices and no alerts, so the
server stops instead of running half blind. Either:
  - give it the key the file was saved with again (OPENCHART_SECRET_KEY, or for
    the Windows service the key file it names), then start it; or
  - move secrets.enc out of the data folder (keep it until the server works),
    start the server, and enter the tokens again in Settings: they are saved
    with the current key.
Everything else in the data folder (history, drawings, alerts) is untouched.
"""


async def _cmd_reset_password(config: Config, args: argparse.Namespace) -> int:
    """Recovery from the server's own console: set a new password, log everyone out."""
    import getpass

    from openchart.api.auth import Auth, PasswordError

    # Checked before asking for the password: the server must be stopped.
    async with _database(config, writes=True, what="run openchart reset-password") as db:
        first = getpass.getpass("New password: ")
        if getpass.getpass("Repeat it: ") != first:
            print("The two passwords differ.", file=sys.stderr)
            return 1
        try:
            await Auth(db.conn, SettingsStore(db.conn)).set_password(first)
        except PasswordError as exc:
            print(exc, file=sys.stderr)
            return 1
    print("Password changed. Every browser has been logged out.")
    return 0


def _data_dir(config: Config, args: argparse.Namespace) -> Config:
    return replace(config, data_dir=Path(args.data_dir)) if args.data_dir else config


async def _cmd_backup(config: Config, args: argparse.Namespace) -> int:
    from openchart.backup import BackupError, backup, human_size

    config = _data_dir(config, args)
    try:
        result = await asyncio.to_thread(
            backup, config.data_dir, Path(args.out) if args.out else None, ticks=not args.no_ticks
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backup written: {result.path}")
    print(f"  {human_size(result.size)}, {result.files:,} files: the database (checked)", end="")
    print(f", {result.tick_files:,} tick files" if result.tick_files else "", end="")
    print(", your indicators and settings files.")
    if result.has_secrets:
        print(
            "  secrets.enc is in it, encrypted; its key is not. Keep the key (the service's "
            "secret.key or OPENCHART_SECRET_KEY) to restore the tokens, or enter them again."
        )
    if result.skipped:
        print(f"  {len(result.skipped)} tick part file(s) were merged during the copy; skipped.")
    print("To restore: stop the server, unzip into an empty data folder, start the server.")
    return 0


async def _cmd_secrets(config: Config, args: argparse.Namespace) -> int:
    """Where the tokens are kept; move them to the encrypted file for the Windows service."""
    from openchart.config import mask_secret
    from openchart.settings import (
        SECRET_NAMES,
        SECRETS_FILE,
        EncryptedFileSecrets,
        new_secret_key,
    )

    if args.action == "new-key":
        print(new_secret_key())
        return 0
    config = _data_dir(config, args)
    if not config.db_path.is_file():
        print(f"No database in {config.data_dir}.", file=sys.stderr)
        return 1
    if args.action == "to-file":  # writes secrets.enc: the server must be stopped
        with exclusive(config.data_dir, "run openchart secrets to-file"):
            return await _secrets_to_file(config, args)
    async with _database(config, writes=False, what="run openchart secrets status") as db:
        instance_id = await SettingsStore(db.conn).instance_id()

    if args.action == "status":
        try:
            store = open_secrets(config, instance_id)
        except SecretStoreUnavailable as exc:
            print(f"Secrets: unavailable. {exc}")
            return 1
        where = (
            f"encrypted file {config.data_dir / SECRETS_FILE}"
            if isinstance(store, EncryptedFileSecrets)
            else "the Windows Credential Manager of this user"
            if os.name == "nt"
            else "the system credential store"
        )
        print(f"Secrets are kept in: {where}")
        for name in SECRET_NAMES:
            try:
                value = store.get(name)
            except SecretStoreUnavailable as exc:
                print(f"  {name}: cannot read ({exc})")
                return 1
            print(f"  {name}: {mask_secret(value) if value else 'not set'}")
        return 0

    raise AssertionError(f"unknown secrets action {args.action!r}")


async def _secrets_to_file(config: Config, args: argparse.Namespace) -> int:
    """Copy the tokens from this user's credential store into secrets.enc, with a key kept
    in --key-file (created if missing). The install script then locks the key file down to
    SYSTEM and Administrators."""
    from openchart.settings import (
        SECRETS_FILE,
        KeyringSecrets,
        secret_key_from_env,
        secrets_to_file,
    )

    async with await Database.open(config.db_path) as db:  # the caller holds the lock
        instance_id = await SettingsStore(db.conn).instance_id()
    if secret_key_from_env():
        print("OPENCHART_SECRET_KEY(_FILE) is set in this shell; unset it first.", file=sys.stderr)
        return 1
    source = KeyringSecrets(instance_id) if KeyringSecrets.available() else None
    try:
        moved = await asyncio.to_thread(
            secrets_to_file, config.data_dir, Path(args.key_file), source
        )
    except SecretStoreUnavailable as exc:
        print(f"{exc}. Use the key file the encrypted file was made with.", file=sys.stderr)
        return 1
    print(f"Key file: {args.key_file} ({'created' if moved.key_created else 'existing'})")
    print(f"Encrypted secrets: {config.data_dir / SECRETS_FILE}")
    print(f"Copied from the Credential Manager: {', '.join(moved.copied) or 'nothing'}")
    if moved.missing:
        print(f"Not set yet (enter them in the web page's Settings): {', '.join(moved.missing)}")
    return 0


async def _cmd_self_test(config: Config, args: argparse.Namespace) -> int:
    """Check, offline, that every part this install needs works (a frozen build, Docker)."""
    import tempfile

    def password() -> str:
        from argon2 import PasswordHasher

        ph = PasswordHasher()
        assert ph.verify(ph.hash("self-test"), "self-test")
        return "argon2"

    def encryption() -> str:
        from openchart.settings import EncryptedFileSecrets, new_secret_key

        with tempfile.TemporaryDirectory() as tmp:
            store = EncryptedFileSecrets(Path(tmp) / "s.enc", new_secret_key())
            store.set("x", "y")
            assert store.get("x") == "y"
        return "Fernet"

    def ticks() -> str:
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as tmp:
            pq.write_table(pa.table({"ts": [1], "bid": [1.0]}), Path(tmp) / "t.parquet")
            assert pq.read_table(Path(tmp) / "t.parquet").num_rows == 1
        return f"pyarrow {pa.__version__}"

    def credential_store() -> str:
        from openchart.settings import KeyringSecrets

        if not KeyringSecrets.available():
            return "none (use OPENCHART_SECRET_KEY or OPENCHART_SECRET_KEY_FILE)"
        import keyring

        return type(keyring.get_keyring()).__name__

    def timezones() -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/New_York")
        return "America/New_York"

    async def database() -> str:
        from openchart.store.migrate import applied_versions, discover_migrations

        with tempfile.TemporaryDirectory() as tmp:
            async with await Database.open(Path(tmp) / "t.db") as db:
                applied = await applied_versions(db.conn)
        expected = {m.version for m in discover_migrations()}
        assert applied == expected and expected, f"applied {applied}, expected {expected}"
        return f"{len(applied)} migrations applied"

    def indicators() -> str:
        from openchart.indicators._registry import load_indicators

        registry = load_indicators(None)
        assert registry.indicators and not registry.errors, registry.errors
        return ", ".join(registry.indicators)

    def web() -> str:
        from openchart.api.app import WEB_DIR

        assert (WEB_DIR / "index.html").is_file(), f"no web UI in {WEB_DIR}"
        return str(WEB_DIR)

    checks = [
        ("password hashing", password),
        ("encrypted secrets", encryption),
        ("tick files", ticks),
        ("credential store", credential_store),
        ("time zones", timezones),
        ("database", database),
        ("indicators", indicators),
        ("web page", web),
    ]
    failed = 0
    for name, check in checks:
        try:
            result = check()
            if asyncio.iscoroutine(result):
                result = await result
            print(f"  ok    {name}: {result}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"openchart {__version__}: {'all checks passed' if not failed else f'{failed} failed'}")
    return 1 if failed else 0


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
    "backup": _cmd_backup,
    "secrets": _cmd_secrets,
    "self-test": _cmd_self_test,
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
    p = sub.add_parser("stats", help="row counts and coverage, drawings, alerts and layouts")
    p.add_argument("--data-dir", help="the data folder (default data/, from config)")
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
    p.add_argument("--data-dir", help="the data folder (default data/, from config)")
    p = sub.add_parser("run", help="start the server: API, websocket and live feed")
    p.add_argument(
        "--host", help="bind address (default: 127.0.0.1, or 0.0.0.0 if enabled in Settings)"
    )
    p.add_argument("--port", type=int, help="port (default 8000, from config)")
    p.add_argument("--data-dir", help="database and tick folder (default data/, from config)")
    p = sub.add_parser(
        "backup", help="copy the data folder into one zip (safe while the server runs)"
    )
    p.add_argument("--data-dir", help="the data folder (default data/, from config)")
    p.add_argument("--out", help="where to put the zip (default: <data folder>/backups)")
    p.add_argument("--no-ticks", action="store_true", help="leave out the raw tick files")
    p = sub.add_parser("secrets", help="where the OANDA and Telegram tokens are kept")
    p.add_argument(
        "action",
        choices=["status", "new-key", "to-file"],
        help="status: where and which; new-key: print a key for OPENCHART_SECRET_KEY; "
        "to-file: copy them from the Credential Manager into the encrypted file (for the "
        "Windows service)",
    )
    p.add_argument("--data-dir", help="the data folder (default data/, from config)")
    p.add_argument("--key-file", help="to-file: the key file to use or create")
    sub.add_parser("self-test", help="check offline that every part of this install works")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "secrets" and args.action == "to-file" and not args.key_file:
        parser.error("secrets to-file needs --key-file")
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
    except DataFolderInUse as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DATA_IN_USE
    except (ConfigError, AuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
