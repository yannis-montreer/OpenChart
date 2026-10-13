-- Deep history and derived-bar cache (SPEC section 6).

-- H1 candles from the provider, used for 1h and higher timeframes where M1 does not
-- reach. Same layout as candles_m1.
CREATE TABLE candles_h1 (
    source     TEXT NOT NULL,
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    bid_o REAL, bid_h REAL, bid_l REAL, bid_c REAL,
    ask_o REAL, ask_h REAL, ask_l REAL, ask_c REAL,
    mid_o REAL, mid_h REAL, mid_l REAL, mid_c REAL,
    volume     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, instrument, ts)
) WITHOUT ROWID;

-- Complete 4h, D and W bars derived from M1/H1. Always rebuildable: deleting rows only
-- costs a recomputation. Rows are removed whenever the M1 or H1 they came from changes.
CREATE TABLE candles_cache (
    source     TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    bid_o REAL, bid_h REAL, bid_l REAL, bid_c REAL,
    ask_o REAL, ask_h REAL, ask_l REAL, ask_c REAL,
    mid_o REAL, mid_h REAL, mid_l REAL, mid_c REAL,
    volume     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, instrument, timeframe, ts)
) WITHOUT ROWID;
