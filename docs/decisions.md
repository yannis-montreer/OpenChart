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
