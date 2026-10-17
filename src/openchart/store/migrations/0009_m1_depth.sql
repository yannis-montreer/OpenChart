-- Minute history per instrument (storage steps 2, docs/decisions-retention.md): '3m', '1y'
-- or '2y'; NULL is the default (2 years, or OPENCHART_BACKFILL_YEARS). Minutes older than
-- it are deleted once a day; nothing else changes for existing rows.
ALTER TABLE instruments ADD COLUMN m1_depth TEXT;
