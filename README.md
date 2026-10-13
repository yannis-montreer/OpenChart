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
| M1 OANDA provider, storage, backfill | done |
| M2 Aggregation, live candles, API, websocket | done |
| M3 Web chart | done, waiting on owner acceptance checks |
| M4 Setup wizard, login, settings | not started |
| M5 Drawings | not started |
| M6 Alerts and Telegram | not started |
| M7 Indicators, chart types, layouts | not started |
| M8 Packaging and deployment | not started |
| M9 Second instrument (EURUSD) | not started |

## Development setup

Requires Python 3.11 or later, and Node.js 24 for the web interface.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Build the web interface once (and again after pulling frontend changes); the server serves it at http://127.0.0.1:8000:

```bash
cd frontend
npm ci
npm run build
```

For frontend work, `npm run dev` serves it with live reload on http://127.0.0.1:5173 and forwards `/api` and `/ws` to a server on port 8000 (set `OPENCHART_API` to use another). `npm test` runs the frontend unit tests.

Configuration is read from environment variables or a `.env` file in the working directory. Copy `.env.example` to `.env` and fill in your values. `.env` is git-ignored; never commit it.

## Command line

Fill in `.env` first (OANDA practice token and account ID), then:

```bash
openchart test-connection          # one live call, prints OK or the error
openchart backfill XAU_USD         # 2 years of M1 (default); add --years N to change
openchart stats                    # rows, range, % of expected trading minutes covered
openchart gaps XAU_USD             # holes not explained by weekends or the daily break
openchart run                      # start the server: API, websocket and live feed
```

- `backfill` is idempotent and resumable: run it again and it only fetches what is missing. A larger `--years` later extends history backwards.
- `run` starts the server on http://127.0.0.1:8000 (change with `--host`/`--port`). It fills any gap since the last stored candle, streams ticks, builds live candles for every timeframe, saves closed M1 bars and raw ticks, and logs feed status every minute. If the link drops or goes silent for 15 s, it reconnects with backoff and gap-fills from REST. Once a minute it re-fetches the last 15 minutes of M1 from OANDA so stored history matches the broker's candles exactly.
- There is no login until M4, so keep the default `127.0.0.1` for now.
- Run **one** server per OANDA account. OANDA limits concurrent price streams per account (about two on practice); an extra stream request is left unanswered and the log says "no answer to the stream request".
- Open http://127.0.0.1:8000 for the chart (after building the web interface, see above).
- REST and WebSocket reference: [`docs/api.md`](docs/api.md). Try http://127.0.0.1:8000/api/candles?instrument=XAU_USD&timeframe=1h&limit=5 or the interactive docs at http://127.0.0.1:8000/docs.
- `pytest -m live -v` runs a few checks against the real OANDA API using your `.env`.

### What works / what does not

Works:
- OANDA candles (paginated, retried, complete only), pricing stream with heartbeat supervision and reconnect, SQLite M1 storage, idempotent and resumable backfill, gap-fill on start and after reconnect, gap report, Parquet tick storage (M1).
- All timeframes from 1s to 1W. 4h, daily and weekly bars use the 17:00 New York trading day and follow DST; derived daily and 4h bars match OANDA's own candles exactly, checked on both 2025/2026 DST changes (M2).
- Live candles from ticks for every timeframe, REST candles (`mid`, `bid`, `ask`), websocket with ticks, live candles and feed status (M2).

- Web chart (M3): live candles for every timeframe, history that loads further back as you scroll left, crosshair with OHLC legend, Bid/Ask lines with a bar-close countdown, tick volume, mid/bid/ask switch, date ranges, display timezone, dark and light theme, watchlist, connection and feed status, keyboard shortcuts, screenshot, full screen. After a dropped connection (or a server restart) it catches up without reloading, including bars that formed while it was away. Usable on a phone for reading the chart.

Not yet: login and setup wizard (M4; credentials come from `.env` until then), drawings (M5), alerts (M6), indicators and other chart types (M7). Their buttons are visible but disabled.

Known limits:
- Daily and weekly requests resample all stored M1 on each call: about 1.4 s for two years of gold. A cache table can be added if this feels slow in the chart.
- Sub-minute history is only available for periods when the server was running with tick storage on, and REST serves at most the last 3 days of it.

## Project layout

See `SPEC.md` section 2 for the architecture. Design choices that the spec left open are recorded in `docs/decisions.md`.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
