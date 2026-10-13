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
| M3 Web chart | done |
| M4 Setup wizard, login, settings | done |
| M5 Drawings | done, waiting on owner acceptance checks |
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

## First start

Run `openchart run` and open http://127.0.0.1:8000. The setup wizard asks for:

1. a password for the web interface,
2. your OANDA account type, account ID and API token (with a Test connection button),
3. optionally a Telegram bot token and chat ID for phone alerts (with a Send test message button),
4. the first instrument (default XAU_USD).

No file editing is needed. Tokens are stored in the operating system's credential store (Windows Credential Manager), never in a file or the database, and the web interface only ever shows their last four characters. Everything can be changed later in Settings (gear icon, top right): password, broker credentials, Telegram, instruments, tick storage, alert timezone, and access from other devices.

- Opening the wizard from another device before setup asks for a one-time setup code printed in the server's console.
- A login lasts 30 days per browser. Failed logins are rate limited.
- Forgot the password? On the server run `openchart reset-password`.
- Docker or headless Linux without a credential store: set `OPENCHART_SECRET_KEY` (a Fernet key) and tokens go to an encrypted file in the data folder instead.

Configuration can still come from environment variables or a `.env` file (see `.env.example`), for headless installs. Values saved in the web interface take precedence. `.env` is git-ignored; never commit it.

## Command line

Fill in `.env` first (OANDA practice token and account ID), then:

```bash
openchart test-connection          # one live call, prints OK or the error
openchart backfill XAU_USD         # 2 years of M1 (default); add --years N to change
openchart backfill XAU_USD --deep  # also H1 back to the broker's first candle (gold: 2006)
openchart stats                    # rows, range, coverage, H1 depth, cached bars
openchart rebuild-cache            # recompute cached 4h/D/W bars (never needed normally)
openchart reset-password           # set a new web password from the server console
openchart gaps XAU_USD             # holes not explained by weekends or the daily break
openchart repair XAU_USD           # refetch those holes from the broker (fills what it has)
openchart run                      # start the server: API, websocket and live feed
```

- `backfill` is idempotent and resumable: run it again and it only fetches what is missing. A larger `--years` later extends history backwards.
- `run` starts the server on http://127.0.0.1:8000 (change with `--host`/`--port`). It fills any gap since the last stored candle, streams ticks, builds live candles for every timeframe, saves closed M1 bars and raw ticks, and logs feed status every minute. If the link drops or goes silent for 15 s, it reconnects with backoff and gap-fills from REST. Once a minute it re-fetches the last 15 minutes of M1 from OANDA so stored history matches the broker's candles exactly.
- By default the server only accepts connections from this computer. To reach it from your phone or another PC (over Tailscale), turn on Settings, Network, "Allow other devices to connect" and restart the server, or start it with `--host 0.0.0.0`.
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

- Setup, login and settings (M4): first-run wizard with connection tests, single-password login (argon2, 30-day HTTP-only session cookie, rate limited), settings page, instruments added and removed from the UI (history downloads in the background), secrets in the OS credential store and never returned by the API.

- Drawings (M5): horizontal line, trendline, ray and extended line from the left toolbar. Click point 1, click point 2 (or press, drag, release); hover shows the handles; drag a handle or the whole line; Delete removes; right-click for style, label, clone and delete; a floating toolbar sets colour, width and dash. Hold Ctrl while drawing to snap to the bar's open, high, low or close. Drawings are stored on the server and every open browser sees changes at once. The eye hides them; the bin deletes all of them on the instrument.

Not yet: alerts and the Telegram notifier (M6; "Add alert" on a line is shown disabled; the Telegram test button already works), indicators and other chart types (M7). Their buttons are visible but disabled.

- Deep history: 1h, 4h, daily and weekly charts go back as far as the broker has H1 (gold on OANDA: March 2006). The server downloads it in the background on first start. Complete 4h/D/W bars are cached, so these charts load in milliseconds.

Known limits:
- 1m to 30m charts only go back as far as stored M1 (2 years by default, `--years` to change). They cannot be built from H1.
- Sub-minute history is only available for periods when the server was running with tick storage on, and REST serves at most the last 3 days of it.

## Project layout

See `SPEC.md` section 2 for the architecture. Design choices that the spec left open are recorded in `docs/decisions.md`.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
