"""Command line entry point: `openchart <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from openchart import __version__
from openchart.config import load_config
from openchart.store.sqlite import Database


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _cmd_migrate() -> int:
    config = load_config()
    async with await Database.open(config.db_path):
        pass
    print(f"Database ready at {config.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openchart", description="OpenChart server")
    parser.add_argument("--version", action="version", version=f"openchart {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the database")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(load_config().log_level)
    if args.command == "migrate":
        return asyncio.run(_cmd_migrate())
    return 2


if __name__ == "__main__":
    sys.exit(main())
