# OpenChart

Self-hosted charting and alerting server for forex and metals. It runs 24/7 on your own PC, pulls prices from your own broker account, and pushes alerts on drawn lines (including sloped trendlines) to your phone, even when no chart is open.

> **Status:** early development. Nothing here is usable for trading yet. See [What works](#what-works) below.

## Important

- **Independent project.** OpenChart is not affiliated with, endorsed by or sponsored by TradingView, Inc. It uses the open-source TradingView Lightweight Charts library under its Apache 2.0 license (see `NOTICE`).
- **Not financial advice.** OpenChart is a charting and alerting tool. Nothing it shows or sends is a recommendation to trade.
- **No warranty.** Provided "as is" under the Apache License 2.0. Alerts can be late or missed (network loss, broker outages, bugs). Use at your own risk.
- **Your data, your credentials.** OpenChart ships no market data. Each install pulls prices with the user's own broker credentials.

## What works

| Milestone | Status |
|---|---|
| M0 Scaffolding: package layout, config, SQLite migrations, tests, CI | done |
| M1 OANDA provider, storage, backfill | not started |
| M2 Aggregation, live candles, API, websocket | not started |
| M3 Web chart | not started |
| M4 Setup wizard, login, settings | not started |
| M5 Drawings | not started |
| M6 Alerts and Telegram | not started |
| M7 Indicators, chart types, layouts | not started |
| M8 Packaging and deployment | not started |
| M9 Second instrument (EURUSD) | not started |

## Development setup

Requires Python 3.11 or later.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Configuration is read from environment variables or a `.env` file in the working directory. Copy `.env.example` to `.env` and fill in your values. `.env` is git-ignored; never commit it.

```bash
openchart migrate      # create or upgrade the database in data/
```

## Project layout

See `SPEC.md` section 2 for the architecture. Design choices that the spec left open are recorded in `docs/decisions.md`.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
