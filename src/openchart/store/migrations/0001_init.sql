-- Initial schema (SPEC section 5). All times are UTC epoch seconds.

CREATE TABLE instruments (
    instrument      TEXT NOT NULL,
    source          TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    decimals        INTEGER NOT NULL,
    pip_size        REAL NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    hours_json      TEXT,
    PRIMARY KEY (source, instrument)
);

CREATE TABLE candles_m1 (
    source     TEXT NOT NULL,
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    bid_o REAL, bid_h REAL, bid_l REAL, bid_c REAL,
    ask_o REAL, ask_h REAL, ask_l REAL, ask_c REAL,
    mid_o REAL, mid_h REAL, mid_l REAL, mid_c REAL,
    volume     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, instrument, ts)
) WITHOUT ROWID;

CREATE TABLE drawings (
    id          TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    type        TEXT NOT NULL,
    points_json TEXT NOT NULL,
    style_json  TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX drawings_instrument ON drawings (instrument);

CREATE TABLE alerts (
    id             TEXT PRIMARY KEY,
    instrument     TEXT NOT NULL,
    drawing_id     TEXT,
    type           TEXT NOT NULL,
    direction      TEXT NOT NULL,
    trigger_price  TEXT NOT NULL,
    trigger_mode   TEXT NOT NULL,
    timeframe      TEXT,
    frequency      TEXT NOT NULL,
    cooldown_s     INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    condition_json TEXT,
    state_json     TEXT,
    created_at     INTEGER NOT NULL
);
CREATE INDEX alerts_instrument ON alerts (instrument);

CREATE TABLE alert_events (
    id        INTEGER PRIMARY KEY,
    alert_id  TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    price     REAL,
    message   TEXT,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX alert_events_alert ON alert_events (alert_id, ts);

CREATE TABLE layouts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    json       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Non-secret settings only. Secrets live in the OS keyring.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
