-- Storage step 1 (docs/storage-review.md, layout A+C): candle prices as scaled integers
-- and a small integer series key instead of the source and instrument names in every row.
-- About half the size, exactly the same prices: every converted value is checked below
-- and any difference aborts this migration (nothing is changed then).
-- vacuum-after: the old tables' pages are only given back to the disk by VACUUM.

-- One row per (source, instrument) with stored candles. `scale` turns a price into an
-- integer: 10^(decimals + 1) (OANDA's mid has one digit more than bid and ask, e.g.
-- EUR/USD's half-pips); 10^6 for a series without instrument metadata.
CREATE TABLE series (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  instrument TEXT NOT NULL,
  scale INTEGER NOT NULL,
  UNIQUE (source, instrument)
);

INSERT INTO series (source, instrument, scale)
SELECT k.source, k.instrument,
  CASE i.decimals
    WHEN 0 THEN 10 WHEN 1 THEN 100 WHEN 2 THEN 1000 WHEN 3 THEN 10000
    WHEN 4 THEN 100000 WHEN 5 THEN 1000000 WHEN 6 THEN 10000000
    WHEN 7 THEN 100000000 WHEN 8 THEN 1000000000
    ELSE 1000000
  END
FROM (
  SELECT DISTINCT source, instrument FROM candles_m1
  UNION SELECT DISTINCT source, instrument FROM candles_h1
  UNION SELECT DISTINCT source, instrument FROM candles_cache
) k
LEFT JOIN instruments i ON i.source = k.source AND i.instrument = k.instrument
ORDER BY k.source, k.instrument;

CREATE TABLE candles_m1_new (
  series INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  bid_o INTEGER, bid_h INTEGER, bid_l INTEGER, bid_c INTEGER,
  ask_o INTEGER, ask_h INTEGER, ask_l INTEGER, ask_c INTEGER,
  mid_o INTEGER, mid_h INTEGER, mid_l INTEGER, mid_c INTEGER,
  volume INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (series, ts)
) WITHOUT ROWID;

CREATE TABLE candles_h1_new (
  series INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  bid_o INTEGER, bid_h INTEGER, bid_l INTEGER, bid_c INTEGER,
  ask_o INTEGER, ask_h INTEGER, ask_l INTEGER, ask_c INTEGER,
  mid_o INTEGER, mid_h INTEGER, mid_l INTEGER, mid_c INTEGER,
  volume INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (series, ts)
) WITHOUT ROWID;

CREATE TABLE candles_cache_new (
  series INTEGER NOT NULL,
  timeframe TEXT NOT NULL,
  ts INTEGER NOT NULL,
  bid_o INTEGER, bid_h INTEGER, bid_l INTEGER, bid_c INTEGER,
  ask_o INTEGER, ask_h INTEGER, ask_l INTEGER, ask_c INTEGER,
  mid_o INTEGER, mid_h INTEGER, mid_l INTEGER, mid_c INTEGER,
  volume INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (series, timeframe, ts)
) WITHOUT ROWID;

INSERT INTO candles_m1_new
SELECT s.id, c.ts,
  CAST(ROUND(c.bid_o * s.scale) AS INTEGER), CAST(ROUND(c.bid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.bid_l * s.scale) AS INTEGER), CAST(ROUND(c.bid_c * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_o * s.scale) AS INTEGER), CAST(ROUND(c.ask_h * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_l * s.scale) AS INTEGER), CAST(ROUND(c.ask_c * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_o * s.scale) AS INTEGER), CAST(ROUND(c.mid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_l * s.scale) AS INTEGER), CAST(ROUND(c.mid_c * s.scale) AS INTEGER),
  c.volume
FROM candles_m1 c JOIN series s ON s.source = c.source AND s.instrument = c.instrument;

INSERT INTO candles_h1_new
SELECT s.id, c.ts,
  CAST(ROUND(c.bid_o * s.scale) AS INTEGER), CAST(ROUND(c.bid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.bid_l * s.scale) AS INTEGER), CAST(ROUND(c.bid_c * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_o * s.scale) AS INTEGER), CAST(ROUND(c.ask_h * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_l * s.scale) AS INTEGER), CAST(ROUND(c.ask_c * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_o * s.scale) AS INTEGER), CAST(ROUND(c.mid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_l * s.scale) AS INTEGER), CAST(ROUND(c.mid_c * s.scale) AS INTEGER),
  c.volume
FROM candles_h1 c JOIN series s ON s.source = c.source AND s.instrument = c.instrument;

INSERT INTO candles_cache_new
SELECT s.id, c.timeframe, c.ts,
  CAST(ROUND(c.bid_o * s.scale) AS INTEGER), CAST(ROUND(c.bid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.bid_l * s.scale) AS INTEGER), CAST(ROUND(c.bid_c * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_o * s.scale) AS INTEGER), CAST(ROUND(c.ask_h * s.scale) AS INTEGER),
  CAST(ROUND(c.ask_l * s.scale) AS INTEGER), CAST(ROUND(c.ask_c * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_o * s.scale) AS INTEGER), CAST(ROUND(c.mid_h * s.scale) AS INTEGER),
  CAST(ROUND(c.mid_l * s.scale) AS INTEGER), CAST(ROUND(c.mid_c * s.scale) AS INTEGER),
  c.volume
FROM candles_cache c JOIN series s ON s.source = c.source AND s.instrument = c.instrument;

-- The round trip over every stored row: each integer divided by its scale must be the
-- very same double as before, and no row may be lost. A failed CHECK aborts the
-- migration and rolls everything back.
CREATE TEMP TABLE conversion_check (
  what TEXT NOT NULL,
  differences INTEGER NOT NULL
    CONSTRAINT "candle prices did not convert exactly; nothing was changed"
    CHECK (differences = 0)
);

INSERT INTO conversion_check
SELECT 'candles_m1 values', COUNT(*)
FROM candles_m1 c
JOIN series s ON s.source = c.source AND s.instrument = c.instrument
JOIN candles_m1_new n ON n.series = s.id AND n.ts = c.ts
WHERE n.bid_o / CAST(s.scale AS REAL) IS NOT c.bid_o OR n.bid_h / CAST(s.scale AS REAL) IS NOT c.bid_h
   OR n.bid_l / CAST(s.scale AS REAL) IS NOT c.bid_l OR n.bid_c / CAST(s.scale AS REAL) IS NOT c.bid_c
   OR n.ask_o / CAST(s.scale AS REAL) IS NOT c.ask_o OR n.ask_h / CAST(s.scale AS REAL) IS NOT c.ask_h
   OR n.ask_l / CAST(s.scale AS REAL) IS NOT c.ask_l OR n.ask_c / CAST(s.scale AS REAL) IS NOT c.ask_c
   OR n.mid_o / CAST(s.scale AS REAL) IS NOT c.mid_o OR n.mid_h / CAST(s.scale AS REAL) IS NOT c.mid_h
   OR n.mid_l / CAST(s.scale AS REAL) IS NOT c.mid_l OR n.mid_c / CAST(s.scale AS REAL) IS NOT c.mid_c
   OR n.volume IS NOT c.volume;

INSERT INTO conversion_check
SELECT 'candles_h1 values', COUNT(*)
FROM candles_h1 c
JOIN series s ON s.source = c.source AND s.instrument = c.instrument
JOIN candles_h1_new n ON n.series = s.id AND n.ts = c.ts
WHERE n.bid_o / CAST(s.scale AS REAL) IS NOT c.bid_o OR n.bid_h / CAST(s.scale AS REAL) IS NOT c.bid_h
   OR n.bid_l / CAST(s.scale AS REAL) IS NOT c.bid_l OR n.bid_c / CAST(s.scale AS REAL) IS NOT c.bid_c
   OR n.ask_o / CAST(s.scale AS REAL) IS NOT c.ask_o OR n.ask_h / CAST(s.scale AS REAL) IS NOT c.ask_h
   OR n.ask_l / CAST(s.scale AS REAL) IS NOT c.ask_l OR n.ask_c / CAST(s.scale AS REAL) IS NOT c.ask_c
   OR n.mid_o / CAST(s.scale AS REAL) IS NOT c.mid_o OR n.mid_h / CAST(s.scale AS REAL) IS NOT c.mid_h
   OR n.mid_l / CAST(s.scale AS REAL) IS NOT c.mid_l OR n.mid_c / CAST(s.scale AS REAL) IS NOT c.mid_c
   OR n.volume IS NOT c.volume;

INSERT INTO conversion_check
SELECT 'candles_cache values', COUNT(*)
FROM candles_cache c
JOIN series s ON s.source = c.source AND s.instrument = c.instrument
JOIN candles_cache_new n ON n.series = s.id AND n.timeframe = c.timeframe AND n.ts = c.ts
WHERE n.bid_o / CAST(s.scale AS REAL) IS NOT c.bid_o OR n.bid_h / CAST(s.scale AS REAL) IS NOT c.bid_h
   OR n.bid_l / CAST(s.scale AS REAL) IS NOT c.bid_l OR n.bid_c / CAST(s.scale AS REAL) IS NOT c.bid_c
   OR n.ask_o / CAST(s.scale AS REAL) IS NOT c.ask_o OR n.ask_h / CAST(s.scale AS REAL) IS NOT c.ask_h
   OR n.ask_l / CAST(s.scale AS REAL) IS NOT c.ask_l OR n.ask_c / CAST(s.scale AS REAL) IS NOT c.ask_c
   OR n.mid_o / CAST(s.scale AS REAL) IS NOT c.mid_o OR n.mid_h / CAST(s.scale AS REAL) IS NOT c.mid_h
   OR n.mid_l / CAST(s.scale AS REAL) IS NOT c.mid_l OR n.mid_c / CAST(s.scale AS REAL) IS NOT c.mid_c
   OR n.volume IS NOT c.volume;

INSERT INTO conversion_check SELECT 'candles_m1 rows',
  (SELECT COUNT(*) FROM candles_m1) - (SELECT COUNT(*) FROM candles_m1_new);
INSERT INTO conversion_check SELECT 'candles_h1 rows',
  (SELECT COUNT(*) FROM candles_h1) - (SELECT COUNT(*) FROM candles_h1_new);
INSERT INTO conversion_check SELECT 'candles_cache rows',
  (SELECT COUNT(*) FROM candles_cache) - (SELECT COUNT(*) FROM candles_cache_new);

DROP TABLE conversion_check;

DROP TABLE candles_m1;
DROP TABLE candles_h1;
DROP TABLE candles_cache;
ALTER TABLE candles_m1_new RENAME TO candles_m1;
ALTER TABLE candles_h1_new RENAME TO candles_h1;
ALTER TABLE candles_cache_new RENAME TO candles_cache;
