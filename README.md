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
| M1 OANDA provider, storage, backfill | done, waiting on owner acceptance checks |
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

## Command line (M1)

Fill in `.env` first (OANDA practice token and account ID), then:

```bash
openchart test-connection          # one live call, prints OK or the error
openchart backfill XAU_USD         # 2 years of M1 (default); add --years N to change
openchart stats                    # rows, range, % of expected trading minutes covered
openchart gaps XAU_USD             # holes not explained by weekends or the daily break
openchart run                      # gap-fill, then stream live ticks (Ctrl+C to stop)
```

- `backfill` is idempotent and resumable: run it again and it only fetches what is missing. A larger `--years` later extends history backwards.
- `run` fills any gap since the last stored candle, streams ticks, saves them to `data/ticks/` (Parquet), and logs feed status every minute. If the link drops or goes silent for 15 s, it reconnects with backoff and gap-fills from REST.
- Live candle building from ticks comes in M2; until then stored candles advance through gap-fills on start and reconnect.
- `pytest -m live -v` runs a few checks against the real OANDA API using your `.env`.

### What works / what does not (M1)

Works: OANDA candles (paginated, retried, complete candles only), pricing stream with heartbeat supervision and reconnect, SQLite M1 storage, idempotent and resumable backfill, gap-fill on start and after reconnect, gap report, Parquet tick storage.

Not yet: live candles, API, web UI, setup wizard (credentials come from `.env` until M4), alerts.

## Project layout

See `SPEC.md` section 2 for the architecture. Design choices that the spec left open are recorded in `docs/decisions.md`.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
