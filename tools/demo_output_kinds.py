"""Run OpenChart on a fake broker in a scratch data folder, with the demo indicators.

    python tools/demo_output_kinds.py       # http://127.0.0.1:8765, password "demo-password"
    python tools/demo_output_kinds.py --port 8000 --days 30

For looking at how the chart draws every indicator output kind (CI-2 step (d)). It never
touches your data folder, your .env or OANDA: prices are made up (gold around 2000, a noisy
wave), a new tick arrives every half second so the forming bar moves,
and nothing is sent anywhere. The demo indicator files in tools/demo-indicators/ are
copied into <scratch>/indicators/. Stop it with Ctrl+C; the scratch folder is deleted then
unless you gave --data-dir.

Build the web UI first (cd frontend && npm ci && npm run build) so the server has pages to serve.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import uvicorn

from openchart.api.app import create_app
from openchart.config import Config
from openchart.market.instruments import METAL_HOURS
from openchart.providers.base import OHLC, Candle, InstrumentInfo, Tick
from openchart.providers.fake import FakeProvider
from openchart.server import Server
from openchart.settings import MemorySecrets
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

GOLD = InstrumentInfo("XAU_USD", "XAU_USD", "Gold", 3, 0.01, "METAL")
EURO = InstrumentInfo("EUR_USD", "EUR_USD", "EUR/USD", 5, 0.0001, "CURRENCY")
DEMO_FILES = Path(__file__).parent / "demo-indicators"
BASE, SWING, SPREAD = 2000.0, 10.0, 0.2  # a slow wave the price is pulled back to
STEP = 0.45  # one-minute noise, so RSI and bands move like on a real chart


def wave(ts: float) -> float:
    return BASE + SWING * math.sin(ts / 3600)


def step(rnd: random.Random, price: float, ts: float) -> float:
    return price + (wave(ts) - price) * 0.02 + rnd.gauss(0, STEP)


class LiveFake(FakeProvider):
    """The fake broker with a price stream that carries on from the last bar."""

    def __init__(self, *args, start: float, **kwargs):
        super().__init__(*args, **kwargs)
        self.start = start

    async def stream_ticks(self, instruments):  # type: ignore[override]
        self.stream_calls += 1
        rnd, mid = random.Random(), self.start
        while True:
            await asyncio.sleep(0.5)
            ts = time.time()
            mid = step(rnd, mid, ts) if rnd.random() < 0.3 else mid + rnd.gauss(0, 0.08)
            yield Tick("XAU_USD", ts, round(mid - SPREAD / 2, 3), round(mid + SPREAD / 2, 3))


def candles(days: int) -> list[Candle]:
    """Open-market minutes of a noisy price around the wave (seeded: the same every run)."""
    end = int(time.time()) // 60 * 60
    rnd, price, out = random.Random(7), None, []
    for ts in range(end - days * 86400, end, 60):
        if not METAL_HOURS.is_open(ts):
            continue
        o = wave(ts) if price is None else price
        price = c = step(rnd, o, ts)
        high = max(o, c) + abs(rnd.gauss(0, 0.25))
        low = min(o, c) - abs(rnd.gauss(0, 0.25))
        bid = OHLC(*(round(x - SPREAD / 2, 3) for x in (o, high, low, c)))
        ask = OHLC(*(round(x + SPREAD / 2, 3) for x in (o, high, low, c)))
        mid = OHLC(*(round((x + y) / 2, 4) for x, y in zip(bid, ask, strict=True)))
        out.append(Candle(ts, bid, ask, mid, volume=10))
    return out


async def seed(data_dir: Path, history: list) -> None:
    async with await Database.open(data_dir / "openchart.db") as db:
        store = CandleStore(db.conn)
        await store.upsert_instrument("fake", GOLD, METAL_HOURS)
        await store.upsert_m1("fake", "XAU_USD", history)


def setup(port: int, code: str, password: str) -> None:
    body = {
        "password": password,
        "setup_code": code,
        "oanda_environment": "practice",
        "oanda_account_id": "101-001-1-001",
        "oanda_token": "demo-not-a-real-token",
        "instrument": "XAU_USD",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/setup",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
    )
    try:
        urllib.request.urlopen(req, timeout=60).close()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"setup failed: {err.code} {err.read().decode()[:300]}") from err


def prepare(args: argparse.Namespace) -> tuple[Path, bool]:
    """The data folder (a temporary one unless given), with the demo indicator files in it."""
    scratch = args.data_dir is None
    data_dir = Path(args.data_dir or tempfile.mkdtemp(prefix="openchart-demo-"))
    if (data_dir / "openchart.db").exists():
        raise SystemExit(f"{data_dir} already has a database: use a new folder, not real data")
    (data_dir / "indicators").mkdir(parents=True, exist_ok=True)
    for f in DEMO_FILES.glob("*.py"):
        shutil.copy(f, data_dir / "indicators" / f.name)
    return data_dir, scratch


async def main(args: argparse.Namespace) -> None:
    data_dir, scratch = await asyncio.to_thread(prepare, args)
    history = candles(args.days)
    await seed(data_dir, history)

    def provider() -> LiveFake:
        return LiveFake({"XAU_USD": history}, instruments=[GOLD, EURO], start=history[-1].mid.c)

    server = Server(
        replace(Config(), data_dir=data_dir),
        provider=provider(),
        secrets=MemorySecrets(),
        provider_factory=lambda _oanda: provider(),
    )
    http = uvicorn.Server(
        uvicorn.Config(create_app(server), host="127.0.0.1", port=args.port, log_level="warning")
    )
    task = asyncio.create_task(http.serve())
    try:
        while not http.started:
            if task.done():
                await task
            await asyncio.sleep(0.1)
        await asyncio.to_thread(setup, args.port, server.auth.setup_code, args.password)
        print(f"{len(history):,} one-minute bars of made-up gold prices, {args.days} days")
        print(f"Open  http://127.0.0.1:{args.port}   password: {args.password}")
        print(f"Data  {data_dir}{' (deleted on exit)' if scratch else ''}")
        print("Indicators menu: Demo kinds, Demo RSI, Demo bands. Ctrl+C to stop.")
        await task
    finally:
        http.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        if scratch:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--password", default="demo-password")
    p.add_argument(
        "--days",
        type=int,
        default=21,
        help="days of one-minute history (default 21: about 20,000 bars)",
    )
    p.add_argument(
        "--data-dir", help="a new folder to use instead of a temporary one (kept on exit)"
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(p.parse_args()))
