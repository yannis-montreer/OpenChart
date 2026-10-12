# Decisions

Choices made where `SPEC.md` was open or ambiguous (SPEC section 18, rule 7). Newest last.

## Owner decisions (2026-10-13)

- History depth for the first backfill: 2 years of M1. Tick storage on by default. Telegram is the only notification channel for now.
- Python 3.11+ supported; CI runs on 3.11 and 3.13.
- Git workflow: one branch per milestone, reviewed and merged by the owner.
- Real-trading broker: not yet confirmed. If it is an MT5 broker, the MT5 provider moves up right after OANDA.

## M0

- **Build backend: hatchling**, src layout (`src/openchart`). Version lives in `openchart/__init__.py`.
- **Lint: ruff** (lint + format check). No type checker in CI yet; can be added later without code changes.
- **Config: environment variables plus an optional `.env` file** (python-dotenv), not pydantic-settings, to keep dependencies small. Real environment variables override `.env`. The M4 setup wizard will store settings in the database and secrets in the OS keyring; environment variables remain an override for headless and Docker installs.
- **Migrations:** numbered SQL files in `src/openchart/store/migrations/NNNN_name.sql`, applied in order by `openchart.store.migrate`, each in its own transaction, tracked in `schema_migrations`. Applied files are never edited.
- **`candles_m1` is `WITHOUT ROWID`**: the table is keyed and range-scanned by `(source, instrument, ts)`, so clustering on the primary key halves storage and speeds range reads.
- **CI runs on Ubuntu and Windows**, because the production target is a Windows 10 PC.

## M1

- **Stream yields heartbeats too.** `Provider.stream_ticks` yields `Tick | Heartbeat`, not only ticks. Without heartbeats the caller cannot tell a quiet market from a dead connection. Reconnect, backoff and the 15 s heartbeat timeout live in one provider-agnostic wrapper (`providers/stream.py`), so future providers get them for free.
- **Providers speak canonical instrument names** (`XAU_USD`) and map to their own symbols internally; `InstrumentInfo.provider_symbol` records the mapping.
- **Candle pagination uses `from` + `count=5000`** (then `includeFirst=false`) instead of fixed `from`/`to` windows. Same 5000-candle limit, but weekends and holidays cost no empty requests.
- **Backfill keeps stored history contiguous**: the tail grows forward, the head grows backward in windows, each batch committed in order. An interrupted backfill is resumed by running it again; no progress table is needed.
- **Test connection calls the account summary**, not a candles request: it validates both the token and the account ID in one call (a candles request does not check the account ID).
- **Ticks keep sub-second precision** (float epoch seconds in memory, microsecond timestamps in Parquet). Candles stay on integer seconds as the spec says.
- **Tick files are one directory per day**: `data/ticks/{source}/{instrument}/date=YYYY-MM-DD/`, holding `part-*.parquet` files written every minute, compacted into `data.parquet` once the UTC day is over. A single file per day cannot be appended to safely; this layout survives crashes and reads as one hive-partitioned dataset.
- **Trading hours** are stored per instrument (`hours_json`, New York time, with daily breaks). Defaults come from the provider's instrument category (OANDA `METAL`, `CURRENCY`) in `market/instruments.py`. Gold: Sunday 18:00 to Friday 17:00 NY with a 17:00 to 18:00 daily break. Forex: Sunday 17:00 to Friday 17:00 NY.
- **Gap report threshold**: 10 missing minutes during expected trading hours. Single empty minutes are normal (no ticks). Exchange holidays are not modelled, so they appear in the report and need a human look.
- **Until M4, credentials come from `.env`** (`OPENCHART_OANDA_*`). M4 moves them to the OS keyring through the setup wizard.
