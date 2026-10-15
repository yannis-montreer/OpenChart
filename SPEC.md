# OpenChart: Project Specification

Repo: https://github.com/yannis-montreer/OpenChart
License: Apache 2.0
Status: spec v1, build from scratch

This document is the single source of truth for building OpenChart. Read it fully before writing code. Build one milestone at a time (section 16), with tests, and do not skip ahead.

---

## 1. What OpenChart is

An open-source, self-hosted charting and alerting platform that covers the parts of a TradingView Premium account a trader actually uses: live charts, multiple timeframes, indicators, drawing tools, price alerts on drawings (including sloped trendlines) pushed to a phone, saved layouts, and later a backtester.

It is independent software. It is not affiliated with TradingView and must not use its name, logo, icons, fonts, CSS, JS or any other asset.

### Goals
- A server that runs 24/7 and owns all data, drawings and alerts.
- A web UI that connects to the server from any device (laptop, phone, another PC).
- Alerts keep working when no UI is open.
- Start with XAUUSD, but every part of the code is instrument-agnostic (EURUSD must work by configuration only).
- Data source is a plugin (OANDA first, others later), so prices can match the broker the user actually trades with.
- Simple setup for a non-programmer: first-run wizard, no file editing.

### Non-goals (v1)
- No social features, community scripts, ideas feed, screeners, fundamentals or news feed.
- No Pine Script. Strategies are Python.
- No order execution. Execution code, if ever added, is off by default and behind an explicit setting.
- No hosted multi-user SaaS. Single user per server.
- No redistribution of market data. Each user pulls their own data with their own broker credentials.

---

## 2. Architecture

```
 Browser / pywebview .exe (UI)  <--- HTTP + WebSocket (over Tailscale) --->  Server (Python)
                                                                              |
                                      +---------------------------------------+------------------+
                                      |                    |                  |                  |
                                 Provider(s)          Storage            Alert engine       Notifier
                                 (OANDA, ...)     (SQLite + Parquet)     (per tick/candle)  (Telegram)
```

Two parts:

1. **Server (engine):** runs as a background service on an always-on Windows 10 PC. Holds the broker connection, stores history, builds candles, computes indicators, evaluates alerts, sends notifications, serves the REST API, the WebSocket and the static web UI.
2. **Client (UI):** a web app served by the server. Any browser can open it. An optional `.exe` (pywebview) is a thin native window that points at the server address.

The UI holds no durable state. Drawings, alerts, layouts and settings live on the server and are synced to all open clients.

### Tech stack
- Python 3.11+
- FastAPI + uvicorn, asyncio throughout
- httpx (REST) and aiohttp or httpx streaming (OANDA stream)
- SQLite via aiosqlite (WAL mode) for candles M1, drawings, alerts, settings, sessions
- Parquet (pyarrow) for raw ticks, partitioned by day
- pandas for resampling and indicators (numpy where useful)
- argon2-cffi for password hashing, `keyring` for secrets
- python-telegram-bot or plain httpx calls to the Telegram Bot API
- Frontend: TradingView Lightweight Charts (v5.x, Apache 2.0) plus plain JavaScript or light TypeScript, built with Vite. No heavy framework (no React unless clearly needed).
- Packaging: PyInstaller + pywebview for the `.exe`, NSSM for the Windows service, Docker image for the server
- Tests: pytest, pytest-asyncio. CI: GitHub Actions.

### Repo structure
```
OpenChart/
  README.md
  SPEC.md
  LICENSE            (Apache 2.0)
  NOTICE             (Lightweight Charts attribution, see section 14)
  .gitignore
  .env.example
  pyproject.toml
  src/openchart/
    main.py              (app entry, CLI)
    config.py
    api/                 (REST routes, websocket, auth)
    providers/           (base.py, oanda.py, later mt5.py, ctrader.py, dukascopy.py)
    store/               (sqlite.py, ticks.py, migrations/)
    market/              (candle_builder.py, aggregate.py, sessions.py, instruments.py)
    indicators/          (sma.py, ema.py, rsi.py, ...)
    alerts/              (engine.py, geometry.py, notifiers/)
    backtest/            (later)
    web/                 (built frontend assets)
  frontend/              (Vite project: src/, chart/, drawings/, panels/, styles/)
  reference/             (UI reference screenshots, behavior only, see section 12)
  tests/
  packaging/             (pyinstaller spec, nssm install script, Dockerfile)
  .github/workflows/     (ci.yml, release.yml)
```

---

## 3. Core design rules

1. **Instrument is a parameter everywhere.** Every table, route, websocket message and function that deals with market data takes `instrument` (and `source`). Never hardcode XAU_USD, decimals, pip size or session hours.
2. **Per-instrument metadata lives in one place** (the `instruments` table plus provider symbol mapping): display name, provider symbol, price decimals, pip size, trading hours.
3. **One active data source per instrument.** Never mix feeds inside one series. Data is stored per `(source, instrument)`.
4. **Store M1 as the source of truth.** All higher timeframes are derived from M1. Sub-minute candles are built live from ticks.
5. **All timestamps are UTC epoch seconds (integer).** Display timezone is a client setting.
6. **Server owns state.** UI edits go through the API and come back through the websocket.
7. **Secrets never leave the server.** The API never returns a stored token, only a masked form (last 4 chars).
8. **Safe defaults.** Bind to 127.0.0.1 unless configured otherwise, auth required, execution off.

---

## 4. Data providers

### Interface (`providers/base.py`)
```python
class Provider(Protocol):
    name: str
    async def list_instruments(self) -> list[InstrumentInfo]: ...
    async def fetch_candles(self, instrument: str, start: datetime, end: datetime,
                            granularity: str = "M1") -> AsyncIterator[Candle]: ...
    async def stream_ticks(self, instruments: list[str]) -> AsyncIterator[Tick]: ...
    async def test_connection(self) -> TestResult: ...
```
Tick = `(instrument, ts, bid, ask)`. Candle = bid/ask/mid OHLC + volume + complete flag. The rest of the system only sees this interface.

### OANDA provider (first implementation)
- Practice REST: `https://api-fxpractice.oanda.com`, stream: `https://stream-fxpractice.oanda.com`
- Live REST: `https://api-fxtrade.oanda.com`, stream: `https://stream-fxtrade.oanda.com`
- Auth: `Authorization: Bearer <token>`. Practice and live use different tokens and hosts.
- Instrument names use underscores: `XAU_USD`, `EUR_USD`.
- History: `GET /v3/instruments/{instrument}/candles?granularity=M1&price=MBA&from=...&to=...` returns at most 5000 candles per request. Paginate by time. Store only candles where `complete == true`.
- Live: `GET /v3/accounts/{accountId}/pricing/stream?instruments=XAU_USD`. Messages are `PRICE` (ticks) and `HEARTBEAT` (every ~5 s). Ticks arrive as they happen, not every 5 s.
- Reconnect loop with exponential backoff. Treat a missing heartbeat for more than ~15 s as a dead connection and reconnect. After reconnect, gap-fill from REST.
- Respect rate limits. Backfill in chunks with a small delay.

### Future providers (design for them, do not build yet)
- MetaTrader 5 (Python `MetaTrader5` package, needs the terminal on Windows, broker server time offset must be handled)
- cTrader Open API
- Dukascopy (history, tick data)
Each provider defines its own symbol mapping (XAU_USD vs XAUUSD vs GOLD) in its instrument list.

---

## 5. Data model

All times are UTC epoch seconds.

```sql
instruments(
  instrument TEXT, source TEXT, provider_symbol TEXT, display_name TEXT,
  decimals INT, pip_size REAL, enabled INT,
  hours_json TEXT,              -- expected trading hours, for the watchdog
  PRIMARY KEY (source, instrument))

series(
  id INTEGER PRIMARY KEY, source TEXT, instrument TEXT,
  scale INT,                    -- prices are stored as price * scale: 10^(decimals + 1)
  UNIQUE (source, instrument))

candles_m1(                     -- candles_h1 and candles_cache (+ timeframe) alike
  series INT, ts INT,
  bid_o INT, bid_h INT, bid_l INT, bid_c INT,
  ask_o INT, ask_h INT, ask_l INT, ask_c INT,
  mid_o INT, mid_h INT, mid_l INT, mid_c INT,
  volume INT,                   -- tick volume
  PRIMARY KEY (series, ts)) WITHOUT ROWID

drawings(
  id TEXT PRIMARY KEY, instrument TEXT, type TEXT,   -- hline | trendline | ray | extended
  points_json TEXT,             -- [{t: epoch, p: price}, ...]
  style_json TEXT, created_at INT, updated_at INT)

alerts(
  id TEXT PRIMARY KEY, instrument TEXT, drawing_id TEXT NULL,
  type TEXT,                    -- level_cross | line_cross | indicator
  direction TEXT,               -- up | down | either
  trigger_price TEXT,           -- bid | ask | mid
  trigger_mode TEXT,            -- touch | candle_close
  timeframe TEXT,               -- used for candle_close
  frequency TEXT,               -- once | once_per_bar | repeating
  cooldown_s INT, enabled INT,
  condition_json TEXT, state_json TEXT, created_at INT)

alert_events(id INTEGER PRIMARY KEY, alert_id TEXT, ts INT, price REAL,
             message TEXT, delivered INT)

layouts(id TEXT PRIMARY KEY, name TEXT, json TEXT, updated_at INT)
settings(key TEXT PRIMARY KEY, value TEXT)        -- non-secret settings only
sessions(token_hash TEXT PRIMARY KEY, created_at INT, expires_at INT)
closures(source TEXT, instrument TEXT, start_ts INT, end_ts INT,   -- learned holidays
         PRIMARY KEY (source, instrument, start_ts))                -- and early closes
```

Raw ticks: Parquet files at `data/ticks/{source}/{instrument}/date=YYYY-MM-DD.parquet`, written in batches. Enabled by default, switchable in settings.

Use a simple migration system (numbered SQL files) from the start.

---

## 6. Candle building and timeframes

### History
1. First run (and every newly added instrument): download newest to oldest, in the background while the live stream runs. First H1 as far back as the provider has it (1h and higher charts are complete within seconds), then M1 back to the configured depth (default 2 years) into `candles_m1`, page by page. What is stored is always one contiguous block ending at the present. The page shows the progress on the chart, in the watchlist and in Settings.
2. Every later start: read the last stored `ts`, fill the gap since then, then switch to the live stream.
   On every start, stream reconnect and wake-up from a pause (the computer slept), also refill holes inside the last 3 days of M1, not just the tail. `openchart repair <INSTRUMENT>` does the same over the whole stored range.
3. Backfill must be idempotent (upsert) and resumable: an interrupted download continues backward from the oldest stored candle. Where each download finished is stored per instrument.
4. **Deep history tier:** H1 candles (bid, ask, mid) as far back as the provider has them, in `candles_h1` (OANDA XAU_USD: from 2006-03-19). Downloaded once in the background on first start, before M1 (or with `openchart backfill <INSTRUMENT> --deep`), then gap-filled on every start. On by default (`OPENCHART_DEEP_HISTORY`).

### Higher timeframes
Derived on demand with a resample:
- open = first open, high = max, low = min, close = last close, volume = sum.
- Supported: 1m, 5m, 15m, 30m, 1h, 4h, 1D, 1W. Sub-minute (1s, 5s, 15s, 30s) from ticks only.
- **Base data:** 1m to 30m come from M1 only. 1h, 4h, 1D and 1W come from H1 where M1 does not reach and from M1 where it does. The switch is at the first whole hour covered by M1, so the two never overlap. Bars derived from H1 equal bars derived from M1 over the same range (tested against OANDA data, including tick volume).
- **Alignment matters.** Use the same boundaries as OANDA and most forex brokers: the trading day closes at **17:00 America/New_York**, weekly candles start Sunday evening (week aligned to Friday 17:00 New York close). H4 and D bars are anchored to that 17:00 boundary, not UTC midnight. Define this once in `market/sessions.py` and use it everywhere. Handle DST through the New York timezone, not fixed offsets.
- Include a test that compares derived D candles against OANDA's own D candles for a sample range.
- **Cache:** complete 4h, 1D and 1W bars are stored in `candles_cache`, always rebuildable from M1/H1 (`openchart rebuild-cache`). The cache grows as bars close (checked every minute and on each request); any M1 or H1 write deletes the cached bars it affects, which are then recomputed. The forming bar is never cached; it is computed from base data on each request.

### Price series choice
The API accepts `price=mid|bid|ask`. Charts default to mid. Alerts and backtests choose bid or ask explicitly.

### Live building
The candle builder consumes ticks, maintains the current candle for each subscribed `(instrument, timeframe)`, emits `candle` updates (open candle) and a final `closed` update when the period ends. The M1 candle is persisted when it closes. Do not persist an incomplete candle.

---

## 7. API

All routes under `/api`, JSON. Everything except `/api/health`, `/api/setup/status`, `/api/setup` and `/api/login` requires a valid session.

### REST
- `GET /api/health`, `GET /api/version`
- `GET /api/setup/status`, `POST /api/setup` (first run only)
- `POST /api/login`, `POST /api/logout`
- `GET /api/settings`, `PUT /api/settings` (secrets are write-only, returned masked)
- `POST /api/settings/test/oanda`, `POST /api/settings/test/telegram`
- `GET /api/instruments`, `POST /api/instruments` (enable and start backfill), `DELETE /api/instruments/{instrument}`
- `GET /api/candles?instrument=&timeframe=&from=&to=&limit=&price=`
- `GET /api/indicators` (the catalog with parameter schemas), `GET /api/indicators/{id}?instrument=&timeframe=&price=&params=&from=&to=&limit=`
- `GET/POST/PUT/DELETE /api/drawings`
- `GET/POST/PUT/DELETE /api/alerts`, `GET /api/alert-events`
- `GET/POST/PUT/DELETE /api/layouts`
- `GET /api/status` (provider state, last tick age per instrument, backfill progress)

### WebSocket `/ws`
Auth by the session cookie. First message from the server is `hello` with `{api_version, server_time}`.

Client to server:
- `subscribe {instrument, timeframe}`, `unsubscribe {...}`, `ping`

Server to client:
- `tick {instrument, t, bid, ask}`
- `candle {instrument, timeframe, candle, closed}`
- `drawing_changed {op: create|update|delete, drawing}`
- `closures {instrument, closures}` (learned market closures changed, section 9)
- `alert_changed {op, alert}`
- `alert_triggered {alert, event}`
- `alert_event {event}` (delivery state of an event changed)
- `layouts_changed {layouts}`
- `status {provider_state, instruments: {...}}`

Reconnect behavior (client): on reconnect, refetch candles since the last known timestamp and resume, no full redraw.

Include `api_version` in the handshake. The client warns if it is older than the server.

---

## 8. Authentication, setup and settings

Goal: the simplest possible UX for one user.

1. **First-run wizard** (shown when no password is set). Fields: app password, OANDA environment (practice or live), OANDA token, account ID, optional Telegram bot token and chat ID, initial instrument (default XAU_USD). Each credential has a **Test connection** button that makes a live call (OANDA: one candles request, Telegram: send a test message) and shows green or red with the error text.
2. **Login:** a single password, stored as an argon2 hash. On success the server sets an HTTP-only, SameSite session cookie valid for 30 days. No usernames, email or OAuth in v1.
3. **Settings page:** change password, rotate OANDA token, change account (refused while price history is stored: another environment or account is another price feed, rule 3 in section 3; the message says to use a new data folder), edit Telegram details, add or remove instruments, toggle tick storage, set display timezone. Same Test buttons.
4. **Secrets storage:** OANDA token and Telegram token go to the OS credential store through `keyring` (Windows Credential Manager). Fallback: encrypted file with a key from an environment variable. Never stored in plain text in SQLite, never logged, never returned by the API (masked, last 4 chars).
5. **Network:** server binds to `127.0.0.1` by default. A setting (or `--host`) allows `0.0.0.0` for Tailscale use. See section 15.
6. Rate-limit login attempts.

---

## 9. Alerts

The alert engine runs inside the server, independent of any open UI.

### Alert types
- **level_cross:** price crosses a horizontal level.
- **line_cross:** price crosses a drawn line (trendline, ray, extended line), sloped or horizontal.
- **indicator:** condition on an indicator (RSI above X, EMA cross). Milestone 7, after indicators exist.

### Line geometry
A line is two anchor points `(t1, p1)` and `(t2, p2)` in UTC epoch seconds and price. Its slope is measured in **trading time** `T(t)`: a clock that runs only while the instrument's market is open (from its trading hours in the `instruments` table, on their local clock, so DST is handled) and stops during weekends and daily breaks. Not bar index (which would depend on the timeframe) and not raw clock time (which would make lines drop a weekend's worth at every weekend). The line the user sees and the line the alert checks are identical, on every timeframe. Instruments without trading hours fall back to clock time.
```
line_price(t) = p1 + (p2 - p1) * (T(t) - T(t1)) / (T(t2) - T(t1))
```
The two anchors must have trading time between them. `T` is implemented twice, in `market/sessions.py` (server, alerts) and `frontend/src/drawings/tradingTime.ts` (drawing); a shared fixture test keeps them identical.

**Learned closures.** The trading hours do not know holidays or early closes. A hole in stored M1 of at least 30 minutes of expected trading time that the broker has no candles for, even when asked again by the repair (section 6), is stored in `closures`, and `T` also stops during it. Shorter holes are a few missing broker minutes and are ignored. They are learned once over the whole history (in the background on the first start, or with `openchart repair`) and then by the recent repair as they happen; the browser gets them with the instrument and a `closures` websocket message. A line drawn across a future holiday therefore shifts slightly beyond it once the holiday has passed.

**On the chart.** The x axis counts bars. Past the last bar, the empty area is counted in the slots of the bars still to come: slots without trading time (weekends, closures) are skipped, and 4h, 1D and 1W slots follow the 17:00 New York alignment through DST changes. Inside a slot, positions are measured in trading time. So a line is straight on screen in the empty area as well; on gold's 4h chart the bar holding the daily break advances it 3/4 as far, as it does on real bars.
- `trendline` (segment): only valid for `t1 <= t <= t2`.
- `ray`: valid for `t >= t1` (in the direction of point 2).
- `extended`: valid for all `t`.
- `hline`: slope zero.

### Cross detection
For each alert keep the previous signed distance `s_prev = price_prev - line_price(t_prev)`. On each evaluation compute `s_now = price - line_price(t)`. A cross happens when the sign changes (`s_prev <= 0 < s_now` is an up-cross, `s_prev >= 0 > s_now` is a down-cross). Respect the alert's `direction`. A price that jumps across the line between two ticks (gap) counts as a cross.

### Trigger options
- **trigger_price:** `bid`, `ask` or `mid`.
- **trigger_mode:** `touch` evaluates on every tick. `candle_close` evaluates when a candle of the alert's `timeframe` closes, using that candle's close against the line value at the candle close time.
- **frequency:** `once` (disable after firing), `once_per_bar`, `repeating`.
- **cooldown_s:** minimum seconds between firings, to avoid spam when price chops around the line.

### Re-arming
When a drawing or alert is edited (a line dragged, a level changed), reset `s_prev` to none and require one fresh observation before the alert can fire. A line dragged past the current price must not count as an instant cross.

### Missed time: sleep, outages, restarts
The server may be frozen (the computer slept), cut off from the feed, or stopped. A tick that arrives after more than 2 minutes of trading time without observations opens a gap: live ticks are kept aside, the stored M1 history is repaired (section 6), the M1 candles of the missed minutes are replayed through the same checks (each candle as open, low and high in the likely order, close), then the kept ticks, and the engine is live again. Fires found this way are marked late (`wake`, `reconnect` or `restart`) in the event log, the Telegram message and the app. After a freeze the server also sends one Telegram message: frozen from X to Y, how many alerts fired late, alerts from that period may have been late. A deleted line deletes its alerts. An alert can expire (`expires_at`).

### Alert dialog (UI)
Laid out like the owner's reference recording: condition (Price; Crossing, Crossing Up, Crossing Down; the line, or a price level), trigger, expiration (default 30 days, or never), message, notifications (Telegram; toast and sound in open browsers), and the price checked (bid, mid, ask). Trigger choices map to the options above: Once only (`touch`, `once`), Once per bar (`touch`, `once_per_bar` on the chart's timeframe), Once per bar close (`candle_close`, `repeating`), Once per minute (`touch`, `repeating`, `cooldown_s` 60). Expiration has a "Never" box next to the date. Edit has a delete button with a confirmation; deleting an alert leaves its line. Opened from a line's right-click menu or style toolbar, the top-bar Alert button (the selected line, or a level at the current price) and Alt+A; on a line that already has alerts these open its alert for editing (the menu entry reads "Edit alert"), or a short list to pick from when it has several. A line with alerts shows a small bell above it (filled while an alert is active, struck through when all are stopped, expired or done; clicking it opens the alert) and the same state on the bell in its style toolbar; the right column has an alerts list (stop, restart, edit, delete) and an event log with delivery state.

### Notifications
- Telegram bot message (default channel). Content: instrument, alert name, trigger price, line value, time (in the configured timezone).
- Notifier is an interface, so ntfy and Pushover can be added later.
- Failed deliveries are retried and recorded in `alert_events.delivered`.

### Watchdog
- On server start, send an "engine started" message.
- If no ticks arrive for N minutes (default 5) during the instrument's expected trading hours, send a "feed stalled" message. Resolve it with a "feed recovered" message.
- A silent dead alert engine is the worst failure for this tool. Treat the watchdog as required, not optional.

---

## 10. Drawings

- Stored on the server, synced to all clients over the websocket.
- v1 types: horizontal line, trendline (two points), ray, extended line.
- Fields: instrument, type, points, style (color, width, dash), optional label.
- UI behavior: click to start, click to finish (or drag), drag handles to move, drag the body to move the whole line, select, delete, right-click menu with "Add alert on this line".
- A drawing with an attached alert shows a small bell indicator and the alert state.
- Later (not v1): rectangles, Fibonacci, channels, text labels.

Implement drawings in the frontend with Lightweight Charts custom primitives. The server only stores geometry.

---

## 11. Indicators and chart types

### Indicators (milestone 7)
Computed **server-side in Python** so the chart and the alerts use identical numbers. Sent as series aligned to candles, in the price type the chart shows (mid, bid or ask).
- Each indicator is one Python file (docs/indicators.md): name, parameters with defaults and types, overlay or own pane, outputs, a compute function and an optional warm-up. The server discovers the built-in files and those in `<data dir>/indicators/` at startup; never uploaded through the UI. The Indicators menu and the settings dialog (Inputs and Style tabs) are generated from the parameter schemas.
- EMA: alpha = 2 / (length + 1), seeded with the simple average of the first `length` values (as TradingView's `ta.ema`).
- First set: SMA, EMA, RSI, MACD, Bollinger Bands, ATR.
- Indicators are composable functions, so indicator-on-indicator works.
- **Multi-timeframe indicators** (for example H1 EMA shown on an M5 chart): compute on the higher timeframe, then map to the lower timeframe using only values from **closed** higher-timeframe bars. No lookahead. Add a test that proves it.
- Volume: forex and gold feeds give tick volume only. Label it as tick volume in the UI.

### Chart types
Candles (default), bars, line, area, Heikin Ashi. Renko and range bars later.
Heikin Ashi is display only, computed in the browser from the real bars: drawings, alerts, the legend values and the last-price label keep the real prices. The bar still forming is recomputed from the real bar on every update (its open is fixed by the previous closed bar; close, high and low move) and is final when the real bar closes.

### Layouts
Saved per user on the server: instrument, timeframe, indicators and their parameters, chart type, price type, theme, panel visibility. A "default layout" loads on start; without one, the chart opens as the browser last left it.

---

## 12. UI specification

Aim: a fast, clean chart workstation with the interaction quality people like in the TradingView UI, built from scratch with its own look.

### Legal and design rules (important)
- Do not copy TradingView's CSS, JavaScript, SVG icons, fonts, images or branding, and do not read or scrape their site's DOM or styles.
- Use an open icon set (Lucide), an open font stack (system UI fonts or Inter), and an original color palette.
- Conventions that are universal are fine: green and red candles, dark background, left tool column, top timeframe bar, crosshair.
- `reference/` contains screenshots and screen recordings provided by the owner. Use them to understand layout proportions and interaction behavior only. Implement everything with original code and assets.
- Show the Lightweight Charts attribution link on the chart page (required by its license).

### Layout
- **Top bar:** instrument selector, timeframe buttons (1s to 1W), chart type, indicators button, alerts button, layout menu, settings, connection status dot.
- **Left toolbar:** cursor, horizontal line, trendline, ray, extended line, delete all, show or hide drawings.
- **Center:** the chart, with crosshair, OHLC legend (updates on hover), price scale on the right, time scale at the bottom, indicator panes below the main pane.
- **Right panel (collapsible):** alerts list (active, triggered, edit, disable, delete), watchlist (single instrument for now, built to hold several).
- **Bottom bar:** server time, timezone selector, price type (mid, bid, ask), connection and feed status.
- Dark and light theme, switchable.

### Interactions to get right
- Smooth wheel zoom and drag pan, double-click on the price scale to auto-fit.
- Crosshair with price and time labels.
- Drawing flow: select tool, click anchor 1, click anchor 2, the tool returns to cursor. Escape cancels.
- Drag handles with live preview.
- Magnet: holding **Ctrl** while a drawing tool is active snaps the cursor to the nearest (by price) of the open, high, low or close of the bar under the mouse, using the price type shown (mid, bid or ask), with a small marker at the snap point. It only works while a drawing tool is active. (Replaces an optional snapping setting.)
- Right-click on a drawing: Add alert, Edit style, Delete.
- Creating an alert shows a compact dialog: direction, trigger price (bid, ask, mid), trigger mode, frequency, cooldown, name. Sensible defaults so one click confirms.
- Live candle updates without flicker.
- Keyboard shortcuts for timeframes and tools.
- Mobile browser: usable view and edit for alerts and basic chart reading. Heavy drawing on phone is not a v1 goal.

---

## 13. Backtesting and later features (not in v1 milestones)

Design the data layer so these slot in without refactoring, but do not build them yet:
- **Backtester:** Python, vectorized with pandas or vectorbt-style. Uses bid and ask (spread matters for gold). Strategies are Python classes loaded as plugins from a `strategies/` folder, kept out of the core repo.
- **Bar replay:** step through stored M1 history candle by candle.
- **Paper trading:** simulated fills against the live feed with logs.
- **Economic calendar** with "news in 15 minutes" alerts.
- **More providers:** MT5 first, then cTrader and Dukascopy.
- **More chart types and drawing tools.**
- **Webhook notifier.**

---

## 14. Licensing, attribution and repo hygiene

- `LICENSE`: Apache 2.0.
- `NOTICE`: include the attribution required by Lightweight Charts (Apache 2.0). Keep the visible attribution link on the chart page.
- `README.md` must state: independent project, not affiliated with TradingView; not financial advice; no warranty; use at your own risk.
- Never commit secrets. `.env.example` is committed, `.env` is git-ignored. `data/` (SQLite, Parquet) is git-ignored. Run a secret scan before the first public push.
- Never ship market data in the repo.
- Keep personal trading strategies out of this repo (private repo or `strategies/` ignored by git).

---

## 15. Deployment

### Server (Windows 10, always on)
- Install as a Windows service with NSSM (`packaging/windows/install-service.ps1`), auto-start on boot, restart on failure. The service runs as LocalSystem and reads the tokens from the encrypted file (`secrets.enc`), keyed by `C:\ProgramData\OpenChart\secret.key` (SYSTEM and Administrators only); the installer copies tokens saved in the owner's Credential Manager into it (docs/decisions.md, M8). `openchart backup` copies the data folder safely while the server runs.
- Disable sleep. Expect Windows Update reboots, so the service must come back on its own and the watchdog message tells the user it happened.
- Windows Firewall: allow inbound on the app port only from the Tailscale range (100.64.0.0/10).

### Remote access: Tailscale
- Install Tailscale on the server and on each client device, same account.
- Enable MagicDNS, enable "Run unattended" on the server, and disable key expiry for the server machine.
- Start the server with `--host 0.0.0.0` (or the Tailscale IP), reach it at `http://<machine-name>:8000`.
- Do not forward ports on the router. The app password is a second lock on top of Tailscale.

### Packaging
- **Web:** the server serves the built frontend, so any browser works with no install.
- **Server for Windows:** the server as a PyInstaller folder (`openchart\openchart.exe`) in a zip with the service scripts and NSSM, so no Python is needed.
- **.exe viewer:** PyInstaller + pywebview wrapper that opens the server URL in a native window. Server address is configurable on first launch. Needs the WebView2 runtime (explained to the user if missing). Note that unsigned executables trigger SmartScreen warnings, mention it in the README.
- **Docker:** `Dockerfile` and `docker-compose.yml` for the server, data in a mounted volume.
- **GitHub Actions:** `ci.yml` runs lint and tests on every push, builds the Windows package and the Docker image and smoke-tests both. `release.yml` (manual run or a `v*` tag) builds the zips, wheel and sdist and, for a tag, a draft release the owner publishes.
- Every artifact carries LICENSE and NOTICE; the Windows zips also THIRD-PARTY-LICENSES.txt.

---

## 16. Milestones

Build in order. After each milestone: run tests, update the README, commit. Do not start the next one until the acceptance criteria pass.

**M0: Scaffolding**
Repo skeleton, `pyproject.toml`, LICENSE, NOTICE, `.gitignore`, `.env.example`, README stub, pytest setup, CI workflow, migration system.
Accept: `pytest` and CI pass on an empty test suite, lint clean.

**M1: Provider interface, OANDA, storage, backfill**
Provider protocol, OANDA provider (candles, stream, test connection), SQLite storage, CLI `openchart backfill XAU_USD --years 2`, gap-fill on start.
Accept: backfill is idempotent (running twice adds no rows), no gaps beyond market closures, row counts are plausible, reconnect works when the network is cut.

**M2: Aggregation, live candles, API and websocket**
Session alignment module, resampling for all timeframes, tick to candle builder, REST candles, websocket with tick and candle messages.
Accept: derived D candles match OANDA D candles on a sample range, sub-minute candles build from live ticks, websocket client receives updates, tests cover alignment across a DST change.

**M3: Web chart**
Vite frontend with Lightweight Charts: live chart, timeframe switching, crosshair, OHLC legend, theme, connection status.
Accept: chart shows history and updates live for XAU_USD, reconnect after a dropped websocket recovers without a full reload.

**M4: Setup, login, settings**
First-run wizard, argon2 login with session cookie, settings page, Test buttons, keyring storage, masked secrets.
Accept: a fresh install is fully configured through the UI with no file editing, the API never returns a stored token, login is rate-limited.

**M5: Drawings**
Horizontal line and trendline (then ray and extended), server persistence, multi-client sync.
Accept: draw, drag, delete, reload, and see the same drawings. Two browsers stay in sync.

**M6: Alerts and Telegram**
Alert engine, line geometry and cross detection, trigger options, re-arming, Telegram notifier, alert UI, event log, watchdog.
Accept: unit tests for geometry (sloped, segment, ray, gap-through, re-arm after edit), an alert on a sloped trendline fires on a phone with the UI closed, watchdog messages fire on feed stall and restart.

**M7: Indicators, chart types, layouts**
First indicator set, multi-timeframe mapping with a no-lookahead test, Heikin Ashi and line charts, saved layouts, indicator alerts.
Accept: indicator values match a reference implementation on test data, the no-lookahead test passes.

**M8: Packaging and deployment**
NSSM install script, PyInstaller + pywebview exe, Dockerfile, release workflow, README quickstart with screenshots.
Accept: clean Windows machine goes from download to a working chart in under 10 minutes using the README only.

**M9: Second instrument acceptance test**
Add EURUSD using configuration and instrument metadata only.
Accept: EURUSD backfills, charts, draws and alerts with zero code changes outside config and metadata. Any hardcoded gold assumption found here is a bug to fix.

---

## 17. Testing requirements

Must have tests for:
- Session alignment and timeframe aggregation, including DST transitions and the weekend gap.
- Backfill idempotency and gap detection.
- Line geometry and cross detection (all line types, up and down, gap-through, re-arm, cooldown).
- Multi-timeframe indicators without lookahead.
- Auth: setup only works once, login rate limit, secrets never in API responses.
- Provider reconnect and heartbeat timeout (with a fake provider).

Use a fake provider that replays recorded ticks for deterministic tests.

---

## 18. Working rules for Claude Code

1. Read this whole file first. Then work milestone by milestone.
2. Keep interfaces (provider, notifier, indicator) small and documented. Write the interface before the implementation.
3. Write tests alongside code, not afterwards.
4. Never hardcode an instrument, a decimal count, a timezone or a host. Use config and metadata.
5. Never write secrets to logs, files in the repo, or API responses.
6. Do not copy anything from tradingview.com. Build UI from the behavior described here and the reference screenshots, with original code, CSS and icons.
7. When something in this spec is ambiguous or conflicts with a technical constraint, make the simplest reasonable choice, note it in `docs/decisions.md`, and continue. Ask only when a choice is hard to reverse.
8. After each milestone, update the README and list what works and what does not.

---

## 19. Open decisions (owner to confirm)

- Which broker is used for real trading? If it is an MT5 broker, the MT5 provider moves up in priority so charts and alerts see the same prices as execution.
- Default history depth for first backfill (spec default: 2 years of M1).
- Notification channel beyond Telegram (ntfy or Pushover), if wanted.
- Whether tick storage should be on by default (spec default: on).
