-- Learned market closures (holidays, early closes): stretches of at least 30 minutes
-- that the trading hours call open but the broker has no candles for. The trading
-- clock (line geometry, SPEC section 9) stops during them.
CREATE TABLE closures (
  source TEXT NOT NULL,
  instrument TEXT NOT NULL,
  start_ts INT NOT NULL,
  end_ts INT NOT NULL,
  PRIMARY KEY (source, instrument, start_ts)
);

-- When the whole stored history was last scanned for closures (NULL: never).
ALTER TABLE instruments ADD COLUMN closures_scanned_at INT;
