-- M9: history downloads newest to oldest and resumes after an interruption; the bar
-- cache refills the bars a download replaces instead of dropping everything after them.

-- Where each download finished (NULL: not finished, it continues backward from the
-- oldest stored candle). H1: the provider's first candle. M1: the start it was asked
-- for (backfill years before the present at that time).
ALTER TABLE instruments ADD COLUMN h1_complete_from INT;
ALTER TABLE instruments ADD COLUMN m1_complete_from INT;

-- Earlier versions downloaded oldest to newest, so stored history is complete.
UPDATE instruments SET h1_complete_from = (
  SELECT MIN(ts) FROM candles_h1 h
  WHERE h.source = instruments.source AND h.instrument = instruments.instrument
);
UPDATE instruments SET m1_complete_from = (
  SELECT MIN(ts) FROM candles_m1 m
  WHERE m.source = instruments.source AND m.instrument = instruments.instrument
);

-- Cached bars that were deleted because the data under them changed, and are not at
-- the end of the cache (there the cache simply grows forward again).
CREATE TABLE cache_dirty (
  source TEXT NOT NULL,
  instrument TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  start_ts INT NOT NULL,
  end_ts INT NOT NULL,
  PRIMARY KEY (source, instrument, timeframe, start_ts)
);

-- Currency pairs pause for the daily rollover, 17:00-17:05 New York (seen on OANDA's
-- EUR_USD: no candles for 4 to 5 minutes every weekday). Rows with the old default
-- currency hours get the break; other hours are left as they are.
UPDATE instruments
SET hours_json = '{"tz": "America/New_York", "open": {"day": "sun", "time": "17:00"}, "close": {"day": "fri", "time": "17:00"}, "daily_breaks": [{"start": "17:00", "end": "17:05"}]}'
WHERE hours_json = '{"tz": "America/New_York", "open": {"day": "sun", "time": "17:00"}, "close": {"day": "fri", "time": "17:00"}, "daily_breaks": []}';
