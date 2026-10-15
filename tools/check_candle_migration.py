"""Check storage step 1 (migration 0007, integer prices) on a COPY of a data folder.

    python tools/check_candle_migration.py <copy of openchart.db>

The file must not be in use: the script copies it to `<name>.before.db`, migrates the
original, then compares. It never connects to a broker. It reports:
- the time the migration took and the file size before and after;
- every stored candle (M1, H1, cached 4h/D/W) read back through the new store,
  compared bit for bit with the float value stored before;
- for each instrument, H1 bars derived from M1 against OANDA's own H1 candles stored
  in the same database, over the hours both cover, with the same count of differences
  before and after the migration (the format must not change what the charts show).
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from openchart.market.aggregate import FULL_COLUMNS, resample_full
from openchart.market.timeframes import TIMEFRAMES
from openchart.store.candles import CandleStore
from openchart.store.sqlite import Database

PRICES = [f"{s}_{x}" for s in ("bid", "ask", "mid") for x in "ohlc"]


def old_rows(path: Path, table: str) -> pd.DataFrame:
    with sqlite3.connect(path) as conn:
        cols = "source, instrument, " + ("timeframe, " if table == "candles_cache" else "")
        return pd.read_sql(
            f"SELECT {cols}ts, {', '.join(PRICES)}, volume FROM {table} ORDER BY 1, 2, 3",
            conn,
        )


def derived_vs_h1(m1: pd.DataFrame, h1: pd.DataFrame) -> tuple[int, int]:
    """(hours compared, hours that differ): 1h bars from M1 against stored H1."""
    bars = resample_full(m1, TIMEFRAMES["1h"]).set_index("ts")
    oanda = h1.set_index("ts")
    hours = bars.index.intersection(oanda.index)
    # The first hour of M1 can be partial (the download starts mid-hour): skip it.
    hours = hours[hours >= -(-int(m1["ts"].min()) // 3600) * 3600]
    cols = PRICES
    a = bars.loc[hours, cols].to_numpy()
    b = oanda.loc[hours, cols].to_numpy()
    return len(hours), int((a != b).any(axis=1).sum())


def size_of(path: Path) -> int:
    return path.stat().st_size


def frame_old(rows: pd.DataFrame, source: str, instrument: str) -> pd.DataFrame:
    part = rows[(rows.source == source) & (rows.instrument == instrument)]
    out = part[["ts", *PRICES, "volume"]].rename(columns={"volume": "v"})
    out.columns = FULL_COLUMNS
    return out.reset_index(drop=True)


async def main(path: Path) -> int:
    before = path.with_suffix(".before.db")
    shutil.copyfile(path, before)
    old = {t: old_rows(before, t) for t in ("candles_m1", "candles_h1", "candles_cache")}
    size = size_of(path)
    counts = ", ".join(f"{t} {len(f):,}" for t, f in old.items())
    print(f"before: {size / 1e6:.1f} MB; rows: {counts}")
    keys = ["source", "instrument"]
    series = sorted(
        set(old["candles_m1"][keys].drop_duplicates().itertuples(index=False))
        | set(old["candles_h1"][keys].drop_duplicates().itertuples(index=False))
    )
    old_derived = {}
    for source, instrument in series:
        m1, h1 = (frame_old(old[t], source, instrument) for t in ("candles_m1", "candles_h1"))
        if len(m1) and len(h1):
            old_derived[(source, instrument)] = derived_vs_h1(m1, h1)

    t0 = time.time()
    async with await Database.open(path) as db:
        print(f"migration (with VACUUM): {time.time() - t0:.1f} s")
        store = CandleStore(db.conn)
        failures = 0
        for table, gran in (("candles_m1", "M1"), ("candles_h1", "H1")):
            for source, instrument in series:
                want = frame_old(old[table], source, instrument)
                got = await store.read_frame(source, instrument, 0, 2**40, None, gran)
                same = len(want) == len(got) and np.array_equal(want.to_numpy(), got.to_numpy())
                failures += not same
                print(f"  {table} {source}/{instrument}: {len(got):,} rows, bit for bit: {same}")
        cache = old["candles_cache"]
        for source, instrument in series:
            for tf in sorted(set(cache.timeframe)):
                mask = (cache.source == source) & (cache.instrument == instrument)
                part = cache[mask & (cache.timeframe == tf)]
                want = part[["ts", *PRICES, "volume"]].to_numpy()
                if not len(want):
                    continue
                got = [
                    await store.read_cache(source, instrument, tf, 0, 2**40, p)
                    for p in ("bid", "ask", "mid")
                ]
                bid, ask, mid = (g.iloc[:, 1:5] for g in got)
                mine = np.column_stack([got[0].ts, bid, ask, mid, got[0].v])
                same = mine.shape == want.shape and np.array_equal(mine, want.astype(np.float64))
                failures += not same
                name = f"{source}/{instrument} {tf}"
                print(f"  candles_cache {name}: {len(mine):,} bars, bit for bit: {same}")
        for (source, instrument), (n, diff_before) in old_derived.items():
            m1 = await store.read_frame(source, instrument, 0, 2**40, None, "M1")
            h1 = await store.read_frame(source, instrument, 0, 2**40, None, "H1")
            n_after, diff_after = derived_vs_h1(m1, h1)
            ok = (n, diff_before) == (n_after, diff_after)
            failures += not ok
            print(
                f"  1h from M1 vs OANDA's H1, {source}/{instrument}: {n:,} hours, "
                f"{diff_before} differ before, {diff_after} after: {'same' if ok else 'CHANGED'}"
            )
    after = size_of(path)
    print(f"after: {after / 1e6:.1f} MB ({(1 - after / size) * 100:.0f}% smaller)")
    print("OK" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]))))
