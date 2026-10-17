"""Measure custom indicator values against the design's targets (design 3.8, CI-5), with
the real worker on synthetic prices in a scratch folder (never your data, never OANDA):

    PYTHONPATH=src python tools/measure_values.py [--runs 30]

Targets, for a typical script (SMA cross, RSI with bands, Bollinger fill) on 5,000 bars
plus warmup: compute at most 20 ms in the worker; a request at most 60 ms end to end at
the 95th percentile with the worker warm; a forming-bar tail update at most 15 ms; the
worker's start at most 2 s. Prints one line per measure and the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from tests.conftest import GOLD, NOW, Harness, run_setup  # noqa: E402
from tests.worker_helpers import real_launchers  # noqa: E402

from openchart.api.app import create_app  # noqa: E402
from openchart.indicators import templates  # noqa: E402
from openchart.indicators._cache import ValueCache  # noqa: E402
from openchart.market.instruments import METAL_HOURS  # noqa: E402
from openchart.providers.fake import synthetic_m1  # noqa: E402
from openchart.store.candles import CandleStore  # noqa: E402
from openchart.store.sqlite import Database  # noqa: E402

SCRIPTS = ("sma_cross", "rsi_bands", "bollinger_fill")
TARGETS = {"compute_ms": 20.0, "request_p95_ms": 60.0, "tail_ms": 15.0, "start_s": 2.0}


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))]


def main() -> int:
    args = argparse.ArgumentParser()
    args.add_argument("--runs", type=int, default=30)
    opts = args.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="openchart-measure-"))
    try:
        from openchart.indicators.worker.runner import Limits

        harness = Harness(folder, indicator_launchers=real_launchers(Limits(timeout_s=10)))

        async def seed():
            async with await Database.open(harness.config.db_path) as db:
                store = CandleStore(db.conn)
                await store.upsert_instrument("fake", GOLD, METAL_HOURS)
                await store.upsert_m1("fake", "XAU_USD", synthetic_m1(NOW - 30 * 86400, NOW))

        asyncio.run(seed())
        ind = harness.config.data_dir / "indicators"
        ind.mkdir()
        for name in SCRIPTS:
            (ind / f"{name}.py").write_text(templates.source(name), encoding="utf-8")
        with TestClient(create_app(harness.server)) as c:
            c.server = harness.server
            assert run_setup(c).status_code == 200
            end = time.monotonic() + 90
            while c.server.isolation is None:
                assert time.monotonic() < end, "the worker did not start"
                time.sleep(0.1)
            runner = c.server.custom.runner
            print(f"worker: {c.server.isolation.level}, started in {runner.start_seconds:.2f} s")
            results = {"start_s": runner.start_seconds}
            q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 5000, "kind": "custom"}
            computes, cold, warm, tails, candles, small = [], [], [], [], [], []
            cq = {k: v for k, v in q.items() if k != "kind"}
            for _ in range(opts.runs):  # the bars alone: what every chart request costs
                t = time.perf_counter()
                assert c.get("/api/candles", params=cq).status_code == 200
                candles.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                assert c.get("/api/candles", params={**cq, "limit": 2}).status_code == 200
                small.append((time.perf_counter() - t) * 1000)
            print(
                f"/api/candles alone, 5,000 bars, p95: {p95(candles):.1f} ms; "
                f"2 bars: {statistics.median(small):.1f} ms"
            )
            for name in SCRIPTS:
                assert c.get(f"/api/indicators/{name}", params=q).status_code == 200  # warm up
                for _ in range(opts.runs):
                    c.server.values_cache = ValueCache()  # a miss: the worker computes
                    t = time.perf_counter()
                    r = c.get(f"/api/indicators/{name}", params=q)
                    cold.append((time.perf_counter() - t) * 1000)
                    assert r.status_code == 200, r.text
                    assert len(r.json()["t"]) == 5000
                    computes.append(runner.last_job_s * 1000)  # the exchange with the worker
                    t = time.perf_counter()
                    assert c.get(f"/api/indicators/{name}", params=q).status_code == 200
                    warm.append((time.perf_counter() - t) * 1000)
                    c.server.values_cache = ValueCache()
                    tail = {**q, "limit": 2}
                    t = time.perf_counter()
                    assert c.get(f"/api/indicators/{name}", params=tail).status_code == 200
                    tails.append((time.perf_counter() - t) * 1000)
            results["compute_ms"] = statistics.median(computes)
            results["request_p95_ms"] = p95(cold)
            results["tail_ms"] = statistics.median(tails)
            print(
                f"compute in the worker (exchange), median: {results['compute_ms']:.1f} ms "
                f"(max {max(computes):.1f})"
            )
            print(f"request, worker computes, p95: {results['request_p95_ms']:.1f} ms")
            print(f"request, from the cache, p95: {p95(warm):.1f} ms")
            print(f"forming-bar tail (2 bars + warmup), median: {results['tail_ms']:.1f} ms")
            peak = getattr(getattr(runner._live, "handle", None), "job", None)
            if peak is not None and sys.platform == "win32":
                from openchart.indicators.worker.windows import job_peak_mb

                print(f"worker memory, peak so far: {job_peak_mb(peak):.0f} MB")
        failed = [k for k, limit in TARGETS.items() if results[k] > limit]
        for k, limit in TARGETS.items():
            print(
                f"  {k}: {results[k]:.2f} (target <= {limit:g}) {'MISSED' if k in failed else 'ok'}"
            )
        return 1 if failed else 0
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
