-- Indicators per chart, on the server (CI-3, decision D1; docs/custom-indicators-design.md 1.2).
-- A chart is (source, instrument). Instances are in legend order (position); `kind` keeps
-- built-in and custom ids apart; `params` is JSON.
CREATE TABLE chart_indicators (
    source        TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    position      INTEGER NOT NULL,
    uid           TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('builtin', 'custom')),
    indicator     TEXT NOT NULL,
    params        TEXT NOT NULL,
    visible       INTEGER NOT NULL DEFAULT 1,
    price_labels  INTEGER NOT NULL DEFAULT 1,
    legend_values INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, instrument, uid),
    UNIQUE (source, instrument, position)
);
CREATE INDEX chart_indicators_definition ON chart_indicators (kind, indicator);

-- One row per chart whose list was ever set: a browser's old list is imported only onto
-- a chart without one, and a write may name the revision it read.
CREATE TABLE chart_indicator_revisions (
    source     TEXT NOT NULL,
    instrument TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (source, instrument)
);
