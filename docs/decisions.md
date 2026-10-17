# Decisions

Choices made where `SPEC.md` was open or ambiguous (SPEC section 18, rule 7). Newest last.

## Owner decisions (2026-10-13)

- History depth for the first backfill: 2 years of M1. Tick storage on by default. Telegram is the only notification channel for now.
- Python 3.11+ supported; CI runs on 3.11 and 3.13.
- Git workflow: one branch per milestone, reviewed and merged by the owner.
- Real-trading broker: not chosen yet. OANDA is the only provider until the owner decides; no other provider is built before then.
- Frontend toolchain: Node 24 LTS (decided 2026-10-13).

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
- **Candle pagination uses `from` + `count=5000`** (then `includeFirst=false`) instead of fixed `from`/`to` windows. Same 5000-candle limit, but weekends and holidays cost no empty requests. Note: with `includeFirst=false` OANDA counts the skipped candle toward `count`, so a full follow-up page holds 4999 candles. Page length is therefore never used as an end signal; pagination stops only on an empty page, the still-forming candle, or the requested end time.
- **Backfill keeps stored history contiguous**: the tail grows forward, the head grows backward in windows, each batch committed in order. An interrupted backfill is resumed by running it again; no progress table is needed.
- **Test connection calls the account summary**, not a candles request: it validates both the token and the account ID in one call (a candles request does not check the account ID).
- **Ticks keep sub-second precision** (float epoch seconds in memory, microsecond timestamps in Parquet). Candles stay on integer seconds as the spec says.
- **Tick files are one directory per day**: `data/ticks/{source}/{instrument}/date=YYYY-MM-DD/`, holding `part-*.parquet` files written every minute, compacted into `data.parquet` once the UTC day is over. A single file per day cannot be appended to safely; this layout survives crashes and reads as one hive-partitioned dataset.
- **Trading hours** are stored per instrument (`hours_json`, New York time, with daily breaks). Defaults come from the provider's instrument category (OANDA `METAL`, `CURRENCY`) in `market/instruments.py`. Gold: Sunday 18:00 to Friday 17:00 NY with a 17:00 to 18:00 daily break. Forex: Sunday 17:00 to Friday 17:00 NY.
- **Gap report threshold**: 10 missing minutes during expected trading hours. Single empty minutes are normal (no ticks). Exchange holidays are not modelled, so they appear in the report and need a human look.
- **Until M4, credentials come from `.env`** (`OPENCHART_OANDA_*`). M4 moves them to the OS keyring through the setup wizard.

## M2

- **Bar timestamp is the bar's start**, as OANDA does. Monday's daily bar is stamped Sunday 17:00 New York, and the weekly bar Friday 17:00 New York. The chart (M3) labels daily bars by trading date.
- **4h bars are anchored to 17:00 New York** (17, 21, 01, 05, 09, 13 local), like OANDA's H4. Checked against OANDA: derived H4 and D bars match OANDA's candles exactly (bid, ask, mid OHLC and tick volume) across the November 2025 and March 2026 DST changes.
- **Session bounds use New York wall-clock time.** An ambiguous local time (DST fall-back) resolves to its first occurrence. Bar starts never fall in the skipped spring-forward hour, so non-existent times do not arise.
- **Sub-minute, minute and hour bars align to epoch multiples.** New York is a whole number of hours from UTC, so this equals local alignment.
- **Live higher-timeframe bars are "closed M1 bars of the period + the forming M1 bar"**, seeded from stored M1 on subscription: the same arithmetic as the history, so live and history agree (tested).
- **Stored M1 is the broker's.** M1 bars built from streamed ticks are persisted on close, then every minute the last 15 minutes are re-fetched from OANDA and upserted. The stream is sampled, so tick-built bars can miss an extreme; the refresh replaces them within about a minute.
- **The first M1 bar after a start or reconnect is not persisted**: we joined mid-minute, so it is incomplete (SPEC: never persist an incomplete candle). It is still shown live. Until that minute closes, a live higher-timeframe bar can miss a few ticks of it; reloading fetches the official bar.
- **Bars close on the next period's first tick, or by the clock** 3 s after the period ends (grace for clock skew and stream latency). Ticks older than the forming bar are dropped.
- **Mid from ticks is rounded to 9 decimal places** to remove float noise; finer than any instrument's precision, so no per-instrument decimals are needed.
- **`/api/candles` paging:** with `from`, the first `limit` bars from there (page forward); without, the latest `limit` (default 1000, max 5000). A bar starting before `from` is excluded rather than cut. Only the last bar can be `complete: false`.
- **Sub-minute history comes from tick files** (plus ticks not flushed yet), at most 3 days per request.
- **WebSocket `subscribe` takes an optional `price`** (`mid` default, `bid`, `ask`), and candle messages carry it. A tick is sent before the candle update it causes. Each client has a 2000-message queue; a client that falls behind is closed with code 1013 and should refetch and resubscribe.
- **No authentication until M4.** The API is bound to 127.0.0.1 by default, and `run` warns when bound elsewhere. The spec's session requirement applies from M4.
- **`openchart run` now starts the HTTP server** (API, websocket, feed) instead of the bare feed. If the first gap-fill fails (network down at boot), the feed retries every 30 s while the API keeps serving stored data. If the credentials are rejected, the feed stops and `/api/status` shows the error.
- **No candle cache table yet.** Daily and weekly requests resample all stored M1 (about 1.4 s for 2 years of gold); add the optional cache if the chart needs it. (Superseded in M3: see "Deep history and cache".)
- **The derived-vs-OANDA candle check is a live test** (`pytest -m live`), so it runs with the owner's credentials and not in CI. No market-data fixtures are committed (SPEC section 14).

## M3

- **Frontend stack:** Vite 8 + TypeScript 7, Lightweight Charts 5.2, Lucide icons, Vitest, Node 24 (`frontend/.nvmrc`). Exact versions are pinned. No UI framework: the shell is plain DOM code.
- **The build is not committed.** `npm run build` writes to `src/openchart/web/` (git-ignored, but included in the wheel). Without a build the server shows a one-page hint at `/`. Release packaging (M8) builds it.
- **Look and feel:** the layout follows the owner's reference screenshots (top bar, left tool column, chart, bottom bar, right strip with a collapsible watchlist, dark navy chart on near-black chrome, compact menus). Palette, CSS, icons (Lucide) and code are original. The system UI font stack is used, so nothing loads from the internet.
- **Lightweight Charts attribution** is its built-in logo link (`attributionLogo: true`), plus links in the About popover and `NOTICE`. Lucide's ISC licence (and MIT for its Feather-derived icons) is in `NOTICE`.
- **`/api/meta`** gives the client the timeframes and the trading-day anchor, so the frontend hardcodes neither.
- **Display timezone:** the chart library draws UTC, so intraday bar times are shifted by the display zone's offset at each bar. In the hour a zone repeats when leaving DST, the repeated bars are dropped from the display; for forex and metals that hour falls on a weekend. Daily bars are shown on their trading date; weekly bars on the Monday of their week.
- **Bid/Ask and countdown labels** are drawn as an HTML stack anchored to the last-price label (Ask above, then the countdown and Bid below). The chart's own labels shift to avoid each other, so overlaying separate labels on them collided.
- **Keeping the chart right after an outage:** on reconnect, and then once a minute while connected, the page re-fetches its last few bars and merges them. Missing bars are inserted (the chart is redrawn in place, keeping the view), and tick-built bars are replaced by the broker's once the server's refresh stores them. An incomplete bar from REST never overwrites the live forming bar, because REST builds it from stored M1 only.
- **The client treats a socket that has been silent for 15 s as dead** (the server sends status every 5 s), which covers laptop sleep and dead Wi-Fi.
- **Client preferences** (instrument, timeframe, price, theme, timezone, favourite timeframes, panel open) are kept in the browser's localStorage. Layouts saved on the server come in M7.
- **Buttons for later milestones** (drawings, alerts, indicators, chart type, settings) are shown disabled with a "coming in Mx" tooltip, so the layout is already the final one.
- **`window.openchart`** exposes the app object for debugging in the browser console.
- **The Python wheel ships the built UI** through `artifacts` in `pyproject.toml`.
- **The broker's limit on concurrent price streams** (practice: about two per account) surfaced during testing. The stream log now says "no answer to the stream request … Brokers limit concurrent price streams" instead of a generic timeout, and the README says to run one server per account.

## M3: deep history and cache (owner request, 2026-10-13)

- **Why:** daily and weekly charts took about 2 s and only had the 2 years of M1. The owner asked for a cache of D, W and 4h and for deeper history from H1 (SPEC section 6 updated).
- **How far OANDA goes:** probed on the practice account, XAU_USD starts on 2006-03-19 at every granularity (M1, M5, M15, H1, H4, D). The deep tier is H1 from that date: about 125,000 rows, downloaded in about 20 s.
- **Why H1 and not more M1:** M1 also exists back to 2006, but that is about 7 million rows (about 1 GB). H1 gives identical 1h, 4h, D and W bars for about 2% of that.
- **The switch from H1 to M1** is at the first whole hour covered by M1 (H1 rows before it, M1 rows from it). Extending M1 further back later moves the switch and invalidates the cache from there automatically.
- **Checked against OANDA:** bars resampled from OANDA H1 equal those resampled from OANDA M1 (live test, D/4h/W, both DST windows, prices and tick volume), and equal OANDA's own D, W and H4 candles in windows from 2006, 2007 (first year of the current US DST rules), 2011 and 2016.
- **The only difference found:** OANDA's first two H1 hours (2006-03-19 20:00 and 21:00 UTC, before that Sunday's 17:00 New York open) form one extra small bar at the very start of our D, W and 4h history; OANDA's own aggregates leave them out. Kept, because it is real data.
- **Cache table** `candles_cache(source, instrument, timeframe, ts, bid/ask/mid OHLC, volume)`, for 4h, 1D and 1W. Only complete bars (period over) are stored; the forming bar is computed on every request. Results are identical to the uncached path (tested).
- **Invalidation instead of bookkeeping:** every M1/H1 upsert deletes cached bars from the period of its earliest candle onward. The cache resumes from its newest bar, so no "built until" state can go stale. A counter bumped before each write lets `extend` notice that data changed while it was computing and drop that chunk (tested).
- **Bars are never cut at `to` either:** `/api/candles?to=` returns every bar that starts before `to`, whole. This is what the cache does naturally; the uncached path was changed to match.
- **1h also uses H1 before M1**, so 1h charts go back to 2006 too. Timeframes below 1h cannot be built from H1 and stay limited to the M1 depth.
- **Speed (2 years of M1 plus 20 years of H1, gold):** first 1000 bars of 4h, D or W in about 12 ms (was about 1.4 to 2 s); each older page about 5 ms; 1h about 110 ms. Full cache build about 2 s.

## M4

- **Secrets:** OS credential store through `keyring` (Windows Credential Manager on the target PC). Entries are named `OpenChart/<instance_id>` per secret; `instance_id` is random per database, so two installs or a test copy on one machine never share secrets. Fallback when no store exists (Docker, headless Linux): `data/secrets.enc`, Fernet-encrypted with the key from `OPENCHART_SECRET_KEY` (SPEC 8.4). An explicit key always selects the file store. New dependency: `cryptography`.
- **Never stored, logged or returned:** the settings table refuses secret names; API responses only carry `{set, masked, source}`; request models hide tokens from their repr; the Telegram client rebuilds error messages so the bot token (part of Telegram's URL) never appears. Tested by crawling every GET route and the logs for the token values.
- **The password hash is in the settings table** (argon2id, argon2-cffi defaults). It is not a secret in the sense of SPEC 8.4, and keeping it there means `openchart reset-password` works from the console without the credential store.
- **Sessions:** 256-bit random token in an HTTP-only, SameSite=Lax cookie, 30 days; the database stores only its SHA-256. `Secure` is set when the page is served over https. A password change ends every other session.
- **Rate limit:** 5 failed logins per client address and 30 in total per 15 minutes; then `429` with `Retry-After`, even for the right password. In memory, so a restart clears it (acceptable for one user; argon2 also makes each guess slow).
- **Who may run setup:** before a password exists anyone reaching the port could claim the server. Setup is allowed from the server computer itself; from anywhere else a 6-character code printed in the server log is required. The wizard's two test buttons follow the same rule.
- **Cross-site requests:** cookie auth plus SameSite=Lax already blocks most of them; in addition, state-changing requests and websocket connections with a foreign `Origin` are refused (websocket hijacking would otherwise work with Lax cookies).
- **Setup checks before saving:** `POST /api/setup` and OANDA changes in Settings call the broker first (list instruments) and save nothing if that fails, so a typo cannot leave the server with a broken feed.
- **Settings take effect without a restart:** `Server.reconfigure()` rebuilds provider and feed (setup, token or account change, tick storage, instruments). Only "Allow other devices" needs a restart, since it changes the listening address; the response says so.
- **Bind address precedence:** `--host`, then `OPENCHART_HOST`, then the Settings switch (`0.0.0.0` when on, `127.0.0.1` otherwise).
- **Environment fallback kept:** `OPENCHART_OANDA_*` from M1 still work for headless installs; anything saved in the UI wins. The wizard shows values found there and lets you keep them.
- **Instruments from the UI:** `POST /api/instruments` validates against the account, enables the instrument and restarts the feed, which downloads its M1 and deep H1 history in the background. Removing hides it and keeps its data.
- **Timezones:** the Settings timezone is the server's (used for alert messages from M6). Each browser keeps its own chart timezone (bottom bar), as SPEC rule 5 makes display timezone a client setting.
- **Added routes not listed in SPEC section 7:** `GET /api/setup/defaults`, `POST /api/password`, `GET /api/instruments/available`. See `docs/api.md`.
- **Recovery:** `openchart reset-password` (console of the server) sets a new password and ends all sessions.

## M5

- **Lines are measured in trading time (owner decision, 2026-10-13; SPEC section 9 updated).** With clock time as first specified, a sloped line visibly dropped a weekend's worth at every weekend (and an hour at gold's daily break on intraday charts), unlike the owner's reference recording. Trading time counts only the instrument's open hours, from its trading hours (New York wall clock), so lines are straight on every timeframe and the M6 alert engine checks exactly the drawn line. Holidays and early closes, which the hours do not model, show as small kinks. Bar-index geometry was rejected because a line would depend on the chart's timeframe.
- **One trading clock, two implementations:** `market/sessions.py: TradingClock` (server, for M6 alerts) and `frontend/src/drawings/tradingTime.ts`. Both precompute open minutes of a local week and use prefix sums, so each call costs one timezone lookup. `tests/test_trading_time.py` regenerates `frontend/src/drawings/trading-time.fixture.json` and fails if it is stale; the frontend test must reproduce every sample exactly.
- **Drawing on an index axis:** the chart's x axis is a bar index. Lines are drawn with one vertex per bar (time is linear inside each bar's slot), which is exact even where a bar contains a closure. Anchors snap to bar start times, like the reference.
- **Ctrl magnet replaces the "optional snapping setting" (owner decision, SPEC section 12 updated).** Holding Ctrl while a drawing tool is active snaps to the nearest of the bar's open, high, low or close (price type shown), with a marker. Not while dragging an existing drawing with the cursor, since the owner limited it to active drawing tools.
- **Interaction follows the reference recording:** click, click (or press, drag, release); the tool returns to the cursor; hollow handles on hover and selection; price and time labels of the points on the axes while selected; floating style toolbar (colour palette or any colour, width 1-4 px, solid/dashed/dotted, label, clone, delete); right-click menu with "Add alert on <line>…" shown disabled until M6, Edit style, label, Clone, Delete; Delete key. Shortcuts: Alt+T trendline, Alt+Y ray, Alt+E extended line, Alt+H horizontal line, Esc cursor.
- **Drawings are per instrument, not per timeframe**, and shown on every timeframe at the same times and prices.
- **Sync:** the server broadcasts every change to all browsers; a browser applies its own change at once and keeps it while dragging even if an older update arrives. After a websocket reconnect the drawings are re-fetched.
- **New: optional `label` column** on drawings (SPEC section 10 lists it), migration 0003.
- **Line geometry module on the server now** (`alerts/geometry.py`): drawing validation uses the trading clock already, and M6 builds cross detection on it.
- **The magnet button in the left toolbar was removed** (the behaviour is Ctrl); the tool tooltips and the shortcuts list mention it.
- **The reference recording** (`reference/2026-10-13 13-08-17.mkv`) stays local and ignored by git; it also shows the alert dialog, kept for M6.
- **Holes inside stored M1 are repaired, not just the tail (found during M5, 2026-10-13).** The owner's chart had two holes today (02:38-04:18 and 04:31-07:50 UTC). The tick files show why: the computer slept with the server running. On waking, the once-a-minute refresh of the last 15 minutes stored candles ahead of the hole, so the gap-fill that the stream reconnect runs (from the newest candle) found nothing to do. A plain restart was never the cause: it gap-fills from the newest candle, which is still before the hole. Now `repair` finds holes with the gap detector (open minutes per the trading hours, at least 2) and refetches only missing candles, never replacing stored ones. The feed runs it over the last 3 days on every start, reconnect and wake-up (a once-a-second loop that took over 30 s); that is usually no request, otherwise one. `openchart repair <INSTRUMENT>` scans everything (holes of 5+ open minutes by default, grouped about 3 days per request). On the owner's data it filled the two holes (297 candles); OANDA has no candles for the other 38 (holidays, early closes, thin reopen minutes).
- **Practice and live share the source name `oanda`.** The owner's database has one source, the saved environment is `practice`, and the account ID (101-004-…) is a practice account, so no live data was mixed in. Nothing stops a later switch to live from appending live candles to the same series, which SPEC section 3 rule 3 forbids. Proposed fix (not built yet): a separate source per environment, or refuse to switch while data exists.
- **The horizontal line after dragging an anchor into the empty area (owner report, 2026-10-13) was drawn on screen only.** Lightweight Charts 5.2.1 maps only whole bar indexes to x and returns 0 for anything else. The last vertex of a line sits on the right edge of the visible range, which is fractional after any scroll or zoom, so the line went on to x = 0 at its end price: a horizontal line back to the left edge. No drawing was created (test: one update, one drawing). Fixed by interpolating between whole bars (`logicalX`). The owner's page still ran the earlier build; the page (index.html) is now served with `Cache-Control: no-cache` and the hashed assets as immutable, so a rebuild takes effect on the next reload.
- **Future slots skip closed time (owner decision).** Past the last bar the chart's slots were calendar steps, while the trading clock stops during closures: lines went flat at every weekend in the empty area on 4h and 1D. Future slots are now the bars still to come by the trading clock, stepped on New York wall time for 4h/1D/1W (DST), and positions inside a slot are measured in trading time, so lines are straight there as well and anchors from another timeframe sit exactly on the line. Remaining by design: on gold's 4h chart the daily-break bar has 3 open hours, and a bar cut by a closure has fewer, so the line advances less there (on real bars too). A bar-index geometry would avoid that but depends on the timeframe (decided against in M5).
- **Learned closures (owner decision: 30-minute minimum, no built-in holiday calendar).** Holes of 30+ open minutes that OANDA has no data for, confirmed by asking again, go to the `closures` table (migration 0004); the trading clock subtracts their open time, identically on the server and in the browser (fixture case `metal_closures`). The first start with this code scans all stored M1 in the background once (about one request per holiday, ~30); `openchart repair` does the same and reports the count. A hole that later gets data stops being a closure. Not done: closures before the M1 range (deep H1 history), and future holidays (unknown until they pass, so lines across one shift slightly afterwards).
- **Changing the OANDA environment or account is refused while price history is stored** (owner decision), in the wizard and in Settings, with a message that names the account the data is from and suggests a new data folder. Token rotation for the same account stays allowed. The stored data is identified by the saved environment and account; the source name stays `oanda`.
- **Short broker gaps are left as they are (owner decision):** the 11-33 minute holes in 2025-11, 2025-12 and 2026-03 are not filled from another source. By the 30-minute rule the 30- and 33-minute ones (2025-12-07, 2025-11-27) count as closures; the shorter ones do not bend lines.
- **Dev server:** `npm run dev` proxied with `changeOrigin`, so every write got a 403 from the origin check; the proxy now keeps the Host header.

## M6

- **One engine, inside the server; the line it checks is the drawing's line** (`alerts/geometry.py` on the instrument's trading clock with learned closures, the function the browser draws with). The weekend test from M5 is repeated for alerts: a sloped line crossing at Monday's trading-time value fires at 98, not at the clock-time 96.
- **Missed time is replayed from history (owner requirement for sleep).** The engine notices the gap itself (a tick after more than 2 minutes of trading time without observations), because at wake-up a tick can arrive before the feed's once-a-second loop sees the freeze; a fire on that tick would have skipped the frozen period. While a gap is open live ticks are kept, the history is repaired, the missed M1 candles are replayed (open, low/high in the likely order, close), then the kept ticks. Weekends and the daily break are not gaps (no trading time). The same path covers feed outages and restarts (the last observation time is saved), labelled `reconnect` and `restart`.
- **Freeze message:** after the feed notices a freeze (a 1-second loop that took over 30 s) the server sends "frozen from X to Y ... N alerts fired late" once the catch-up is done (or after 30 s if the market was closed and there was nothing to replay). The watchdog restarts its timers after a freeze so a freeze is not also reported as a stalled feed.
- **Trigger options follow the reference dialog** (Once only, Once per bar, Once per bar close, Once per minute), mapped to the spec's `trigger_mode`, `frequency` and `cooldown_s`; the API takes the spec's fields. "Greater than / Less than" from the reference were left out: the spec only has crossing alerts.
- **Expiration** (from the reference) is an extra field; expired alerts stop and send "Alert expired" to Telegram.
- **Deliveries:** an event is recorded first, then sent; failures are retried after 5 s, 30 s, 2, 10, 30 and 60 min and the attempts and last error are stored and shown in the log. Without Telegram set up nothing is retried and the log says so. App-only alerts count as delivered. System messages (engine started, stalled, recovered, frozen, expired) are retried in memory only.
- **Watchdog:** "engine started" on every server start, "feed stalled" after 5 minutes without a tick while the market should be open by the trading clock (hours and learned closures; holidays not yet learned still show up), "feed recovered" when ticks are back.
- **A deleted line deletes its alerts** (the reference does the same); restyling a line keeps its alerts armed, moving it re-arms them.
- **Alert dates and numbers are formatted in English** like the rest of the interface, whatever the browser's language.
- **A line opens its existing alert** (owner, after the M6 checks): the bell and the menu entry on a line with alerts edit that alert (a list when there are several) instead of creating another, which is why the edit dialog's delete button was out of reach before: only the alerts panel opened the edit dialog. A line with an alert gets no second one from the line (owner); several can still exist (made before this change, or from the API). The line state is derived in the browser from the alerts it already has (`status`, `drawing_id`), so no extra field on drawings.
- **Expiry is announced on Telegram** ("Alert expired ... no longer watches the price") for alerts that notify Telegram; "Never" is a box next to the date.
- **A websocket joins the broadcasts before it is accepted** (CI failure on the M6 PR, Python 3.13 runner). The server accepted the socket, then checked the session, then registered it for broadcasts; anything broadcast in between was lost. Browsers reload drawings and alerts as soon as the socket opens, so a change landing in that window (an alert expiring, a line edited elsewhere) would not show until the next reload. The server now checks the session and registers the client first, then accepts. Tests wait for websocket messages with a deadline (`tests/ws.py`) instead of reading until a message count runs out, and a test slows the session check to keep this fixed.

## M7 (reduced scope, owner)

- **Deferred by the owner:** multi-timeframe indicators (and their no-lookahead test), alerts on indicators, and the rest of the first set (SMA, RSI, MACD, Bollinger Bands, ATR as indicator files). The mechanism is complete; only EMA ships. The `sma` helper exists for composition but is not an indicator yet.
- **An indicator is a file**, built-in (`src/openchart/indicators/*.py`) or the owner's (`<data dir>/indicators/*.py`), discovered at startup; never uploaded through the UI, because it runs as Python in the server. A file that fails to load is listed with its error instead of stopping the server. The interface is declarations plus `compute(bars, params)` and an optional `warmup(params)` (docs/indicators.md). Colour and width parameters form the Style tab, the others the Inputs tab; the Indicators menu, the settings dialog and the legend row are generated from that, so a new file needs no UI work.
- **Indicator files are loaded by path, under their own module names**, not as `openchart.indicators.<name>`: importing `openchart.indicators.ema` sets the package's `ema` attribute to the module, which replaced the `ema()` helper indicator files import (found while testing a second indicator; a test covers it). For the Windows build (M8) the built-in files must be shipped as data files next to the package.
- **EMA** uses alpha = 2 / (length + 1) and is seeded with the simple average of the first `length` values, like TradingView's `ta.ema`, so the first bars of history match. Warm-up is 10 lengths (+10): the seed's weight is then below 1e-8, so values do not depend on where the chart's history starts. Tested against a written-out reference, by hand, on the first bars of history and per price type.
- **Values follow the chart's bars**: the API reads the same bars as `/api/candles` for the same query plus the warm-up before them, and returns one value per bar. The page fetches values for all bars it holds whenever they change (load, older history, a rewrite after a reconnect) and, while a bar forms, re-fetches the last two values at most once a second (the previous bar may just have closed).
- **Indicator panes**: overlays on the price pane; each other indicator gets a pane below, a quarter of the price pane's height. The Visibility tab, the Offset and Timeframe fields, and smoothing from the reference were left out (owner: no Timeframe for now).
- **Chart types**: the library cannot change a series' type, so the price series is replaced and the drawings layer moves to the new one. Line and area show the close.
- **Heikin Ashi is display only**, computed in the browser from the real bars: close = (O + H + L + C) / 4, open = average of the previous HA open and close (the first bar loaded: of its own open and close), high and low widened to include them. Drawings, alerts, the magnet, the legend values and the last-price label keep the real prices: the HA series hides its own last-value label and a real-price line carries the label. **The forming bar** is recomputed from the real forming bar on every update: its open is fixed (the previous bar is closed), close, high and low move with the price, and it becomes final when the real bar closes. Earlier HA bars never change. Loading older history re-seeds from the new first bar; the seed's influence halves each bar, so the bars on screen do not visibly change.
- **Layouts** store instrument, timeframe, chart type, price type, theme, panel state and the indicators with complete parameters; the default layout (a setting) loads on start, otherwise the chart opens as the browser left it. Saving is explicit (menu or Ctrl+S); every change is broadcast (`layouts_changed`) so other open pages update their menu.
- **No reference screenshot of an indicators list or a chart-type menu** was in `reference/`; those follow the EMA settings dialog and the existing menus.

## M8

- **The Windows service runs as LocalSystem, and its tokens live in the encrypted file, not the Credential Manager.** Credential Manager entries belong to the Windows user who saved them, and LocalSystem has its own (empty) store, so a LocalSystem service cannot read the owner's tokens. The two ways out were to run the service under the owner's account, or to use the encrypted-file store that Docker already uses. Running as the owner means NSSM keeps the owner's Windows password in the service configuration; the service then breaks whenever that password changes, and with a Microsoft account or a PIN sign-in the password Windows wants is the online one. So the service runs as LocalSystem with `OPENCHART_SECRET_KEY_FILE` pointing at `C:\ProgramData\OpenChart\secret.key`. That file is readable by SYSTEM and Administrators only (set with SIDs, so localized Windows works). The install script copies any tokens the owner already saved from the Credential Manager into `<data>\secrets.enc` (`openchart secrets to-file`, run as the owner before the service starts). Tokens entered later in the web page go straight to the file. Anyone with administrator rights on the PC can read the key, which matches what an administrator could do anyway. A backup holds `secrets.enc` but not the key. Tested on the owner's PC with a temporary `OpenChartTest` service (separate data folder, port 8799, no broker): the LocalSystem service reported the token present from the encrypted file, and absent from Credential Manager without the key file. The server now logs at start where its tokens come from and which ones are set, never their values.
- **The service runs a bundled server, not Python.** The release zip has the server as a PyInstaller folder (`openchart\openchart.exe`) plus NSSM 2.24 (public domain; checksum pinned in the release workflow), so a clean PC needs no Python. From a source checkout the same script takes `-Exe .venv\Scripts\openchart.exe`. NSSM settings:
  - start on boot;
  - restart on any exit (5 s, throttled after a failure within 30 s), plus Windows recovery actions in case NSSM itself dies;
  - Ctrl+C on stop, so ticks are flushed (tested: a clean shutdown);
  - logs in `<data>\logs\service.log`, rotated at 10 MB.
  NSSM 2.24 has no `AppKillProcessTree` setting; it already ends the whole process tree.
- **Firewall:** one inbound rule, the app port from `100.64.0.0/10` only, any profile. The server listens on `0.0.0.0` rather than the Tailscale IP, because Tailscale may not be up yet when the service starts at boot. Windows creates broad "allow from anywhere" rules when a program like `python.exe` first listens; those would let other networks in too, so the script lists them and prints the command to disable each, without changing them itself.
- **Sleep:** the script reads the plugged-in sleep and hibernate timeouts through `powercfg /query` and prints the `powercfg /change ... 0` commands. It never runs them.
- **The viewer** is a pywebview window on the server's page; the page and the login cookie are the server's. Details:
  - The address is asked on first launch and stored in `%APPDATA%\OpenChart Viewer\viewer.json`.
  - `private_mode=False` with its own profile folder, so the login survives restarts.
  - It needs the WebView2 runtime. Without it pywebview silently falls back to Internet Explorer's engine, which cannot run the page; the viewer checks the registry first and offers Microsoft's download page or the default browser.
  - It is built as one file, which unpacks itself on each start (about a second).
  - A pywebview pitfall: an object exposed to JavaScript must not hold the window in a public attribute, or pywebview walks the native window object recursively.
- **Docker:** a two-stage build (node for the web UI, `python:3.13-slim`), a non-root user, data in the `/data` volume, a healthcheck on `/api/health`, LICENSE and NOTICE in `/usr/share/doc/openchart`. A container has no credential store, so `OPENCHART_SECRET_KEY` is required (compose refuses to start without it). The host bind address is set in compose and defaults to `127.0.0.1`. Docker is not installed on the owner's PC, so a CI job builds and runs the image: self-test, compose up until healthy, page and API, a backup inside the container, and no answer on other addresses when bound to 127.0.0.1. Nothing is pushed to a registry.
- **`openchart backup`:** SQLite's online backup API in one step (a single read transaction, so one consistent snapshot even while the server writes), `quick_check` on the copy, then a zip with the other files of the data folder. Logs, earlier backups and leftovers are left out. Tick Parquet files are stored uncompressed, since they are already compressed. A tick part file merged away during the copy is skipped; its ticks are in the day's merged file. The zip is named in local time.
- **`openchart self-test`** checks offline that every bundled part works: argon2, Fernet, Parquet, keyring backend, time zones, migrations, indicators, web UI. The build runs it on the frozen server, and so do the CI jobs for the Windows package and the Docker image.
- **Licences:** `packaging/third_party_licenses.py` follows the dependency tree actually bundled and writes `THIRD-PARTY-LICENSES.txt` with every licence text. It covers:
  - the Python packages;
  - Python itself;
  - the web page's npm packages;
  - NSSM;
  - the WebView2 SDK, whose licence text the release workflow takes from the matching NuGet package.
  The build fails on a licence not known to be compatible with Apache 2.0. Result: MIT, BSD, ISC, Apache-2.0, PSF, Zlib/CC0 (inside numpy), MPL-2.0 (certifi, distributed unmodified, which MPL allows), public domain (NSSM). No GPL code is bundled; PyInstaller's bootloader exception covers the built programs. `clr_loader` declares no licence in its metadata; its licence file says MIT. `fancy-canvas` (bundled by Lightweight Charts) declares MIT and ships no licence file; NOTICE now names it. LICENSE, NOTICE and THIRD-PARTY-LICENSES.txt are in both zips and inside both programs (the viewer shows them under OpenChart, About and licences). The wheel carries LICENSE and NOTICE, and so does the Docker image.
- **Release workflow:** it runs by hand or on a pushed `v*` tag, and fails if the tag differs from `__version__`. A tag gives a *draft* release that the owner publishes. Nothing is signed: SmartScreen warns, and the README says how to get past it.
- **Setup page wording:** it says where tokens go: the credential store, or, under the service and Docker, the encrypted file. The public setup-status route gives only the kind of store, no path.
- **History secret scan (2026-10-14):** every commit on every ref (37 commits, 19 refs) was scanned for token, account-ID and key shapes, and for the owner's actual values. Those values were read on the owner's PC from `.env`, Credential Manager and the database, compared in memory, and never printed. None was found. No `.env`, database, key, tick or reference file was ever committed. The pattern hits are test placeholders and the account-ID format example.
- **With M9 merged in:** backups leave out `server.lock` (the lock M9's server holds on its data folder), and `openchart purge` takes `--data-dir` like the other commands, so it can reach the service's `C:\ProgramData\OpenChart\data`. The server folder bundles M9's migration 0006 and lock module like every other package file; the build's smoke test runs the migrations.

## M9

The code was searched for gold assumptions (names, decimals, pip size, price sizes, hours, the first instrument being the only one). Most was already per instrument: decimals and pip size come from the broker (`displayPrecision`, `pipLocation`) and drive the price scale, legend, Bid/Ask labels, watchlist, magnet rounding, drawing labels, the alert dialog and Telegram; hours and learned closures are stored per instrument and drive the trading clock of lines and alerts, the gap report, the closure scan and the watchdog; history, repair, deep H1, the bar cache and tick files are keyed by instrument; the stream takes the list of instruments. Found and fixed:

- **A new instrument stopped the live feed while its history downloaded (bug).** The feed downloaded the missing history of every instrument before it started streaming, one instrument after the other. Adding EUR_USD restarts the feed, so gold's prices and alerts would have stopped for the minutes the two years of M1 take (about 150 requests on OANDA), and a failure in that download stopped the whole feed until its retry. Now instruments with history gap-fill as before (a few requests) and the stream starts; an instrument without history downloads it in the background, extending backward from any live candles already stored, and a failed download is retried every minute without touching the others. It is left out of the gap-fill, repair and bar cache until its download is done.
- **The price scale did not reset when switching instrument (bug).** A price scale dragged by hand on gold (around 2650) stayed in place on EUR/USD (around 1.08), so the chart looked empty. Switching instrument turns autoscale back on, like TradingView.
- **Default alert names rounded the level to 6 significant digits (bug).** An alert made through the API without a name was called "Crossing 2638.72" for 2638.719. It now uses the instrument's decimals ("Crossing 1.07531", "Crossing 2638.719"), as the web dialog already did.
- **Hours could only come from the broker category.** `CURRENCY` and `METAL` have defaults (`market/instruments.py`); a category without one (OANDA's `CFD`) gets no hours, so lines and alerts fall back to clock time. A provider can now report hours in `InstrumentInfo.hours`, which win over the defaults. OANDA does not report them, so EUR_USD gets the currency hours (Sunday 17:00 to Friday 17:00 New York with a 5-minute rollover break at 17:00, added after the real run below: about 119.6 open hours a week, against gold's 115 with its daily 17:00-18:00 break).
- **Test data was gold-sized.** The fake broker's synthetic candles moved by ±10 with a 0.2 spread whatever the price; they now scale with the price (identical for gold), so EUR/USD and the fake index look like themselves.

**Kept on purpose:** the setup wizard suggests XAU_USD as the first instrument and help texts use it as an example of OANDA's names. Bar alignment (4h, D and W on the 17:00 New York trading day) is the same for all instruments: that is how OANDA aligns daily candles for every instrument, so derived bars keep matching the broker's.

**Indicators and the chart settings follow the chart, not the instrument**, as in TradingView: switching keeps the EMA and recomputes it on the new instrument's prices. Drawings, alerts, hours, closures and decimals belong to their instrument. Layouts store the instrument with the rest.

**Proof without a broker:** the tests add EUR_USD and a made-up third instrument (`FAKE_IDX`: 1 decimal, a provider symbol of its own, a category without default hours, hours reported by the provider on Berlin time with a daily break over midnight, and a holiday in its data) through the same path as the Settings page: search, add, background download, closure scan, alerts on its own clock, Telegram with its own decimals. The trading-time fixture shared with the frontend has the third instrument's hours too, so the browser's clock is checked against the server's for them. The weekend straightness test and the 4h bar test are repeated for EUR/USD.

**The real run (owner):** `gaps EUR_USD` showed 937 holes of 2+ minutes, nearly all 4 to 5 minutes at 17:00 New York every weekday (summer: last candle 20:59 UTC, next 21:04; winter 21:59 and 22:04): the rollover. Currency pairs now have a daily break 17:00-17:05 New York in their default hours (a migration adds it to stored rows that have the old default; gold is unchanged). The 2 to 3 minute holes that sometimes follow and other one-offs are not modelled. Effects, each tested: the trading clock (server and browser, shared fixture) stops for those 5 minutes, so a ray across 17:00 is straight (16:59 and 17:05 are adjacent bars); the 17:00 4h bar still opens at 17:00 and holds 235 minutes; `gaps` and `repair` no longer report or refetch the break; the closure scan only looks at 30+ minute holes, so it is unaffected; the alert engine does not treat the break as a missed period, but a stop that also takes trading minutes around it is replayed. The week opens at 17:05 on Sunday. A per-instrument break was already possible: hours are stored per instrument (`instruments.hours_json`), overrides by name go in `HOURS_BY_INSTRUMENT`, and a provider can report hours.

**Downloads newest to oldest (owner, after the real run).** The first real EUR_USD download went oldest to newest: for minutes the chart showed two-year-old prices, a blank stretch and the live price, with a legend change of +8.63% against a stale candle. Now every download (a new instrument, the wizard's first one, an interrupted one) runs newest to oldest in the background: H1 first, all of it (about 30 requests on OANDA, so 1h and higher charts are complete within seconds), then M1 back to the configured years, a page at a time. Every page is committed on its own and is older than everything stored, so the stored range is always one block ending at the present; a restart continues from the oldest stored candle. Where each download finished is stored per instrument (`h1_complete_from`, `m1_complete_from`; the migration marks existing history as complete, since earlier versions downloaded oldest to newest). The closure scan runs once M1 is complete.
- **OANDA backward paging:** `to` + `count`, starting without `to` (the latest candles, ending with the forming one). The candle at `to` may come back and count toward `count`, like the skipped first candle going forward, and the forming candle counts too: both are dropped, so a page can hold 4999, and only an empty page means there is nothing older. A test serves more than 3 pages through the mock (which returns and counts the boundary candle) and checks every complete candle arrives once.
- **The cache refills instead of dropping everything after a write.** A write used to drop every cached 4h/D/W bar from its first candle on, so each page of a backward download would have dropped the whole cache. Now it drops the bars the written range touches, plus the next hour (writing M1 moves the hour where M1 takes over from H1); at the end of the cache they are recomputed as it grows forward, anywhere else the range is recorded (`cache_dirty`) and recomputed on the next read or extend. A test checks after every page of a download, with H1 deliberately unlike M1, that the cache equals an uncached computation, and another that the bar holding the H1/M1 boundary takes H1 up to the boundary hour and M1 from it, each once.
- **A partly downloaded range:** gaps and repair only look between stored candles, so the part not downloaded yet is never a hole (tested after every page), and the closure scan waits for the download.
- **Progress is visible:** a panel on the chart with a progress bar and the oldest date reached (step 1 fills the first tenth, step 2 the rest), a marker in the watchlist's change column, a line in Settings, and "Loading older history…" at the left edge when scrolling past what has arrived (it loads more as it comes). The legend hides the change when the previous bar is further back than a bar and a quiet stretch of trading time (a hole).
- **`openchart purge <INSTRUMENT>`** deletes an instrument with its candles, cache, closures and tick files (drawings and alerts only with `--drawings-and-alerts`). It shows what it would delete unless given `--yes`. The server now holds a lock file in its data folder (`server.lock`, released by the operating system when the process ends), and purge refuses while it is held, whatever port the server uses. A server already running from an older version does not hold it, so stop the server first anyway.

## After M8 (owner's field tests passed, 2026-10-15)

- **Tick-built minutes the refresh missed (bug).** Once a minute the feed re-fetches the last 15 minutes from the broker, replacing the minutes it built from streamed ticks. Minutes that close just before the server stops, the stream drops or the computer sleeps miss those refreshes, and nothing replaced them later: gap fill only adds newer candles and repair only fills holes. The owner's gold had one (2026-10-13 18:00 UTC, a different high and volume), found when 1h bars built from M1 were compared with OANDA's own H1. Two checks now replace them, each one request per instrument:
  - **the hour before a stop (option A):** on every start and reconnect, the hour up to the newest stored minute, before gap fill moves it; on waking, the hour before the pause began;
  - **the previous day (option B):** once a day per instrument, the whole previous UTC day, 15 minutes after midnight when the broker's candles are final, or at the next start if the server was off. The day reached is kept in the `settings` table (`m1_checked_through`), so no migration. Older days are not checked; `openchart repair` remains for holes.
  - Both write only the minutes that differ or are missing, and keep minutes the broker has none for. They log how many they replaced.
- **`openchart stats` showed EUR_USD at 102.9% (bug).** Expected minutes were estimated by sampling the hours once an hour: when the history started 0 to 4 minutes past an hour, every sample fell on 17:0x New York, inside the rollover break, so each day lost an hour of expected minutes but only five of stored ones. Both counts are now exact to the minute (the trading clock for the expected ones, learned closures excluded), the ratio cannot pass 100%, and stored minutes outside the hours are reported apart. 0.4 s for 2.4 million minutes.
- **A changed secret key left a dead server (bug, found in the M8 Docker test).** With `OPENCHART_SECRET_KEY` (or the service's key file) changed, the start failed, but the database connection it had opened kept the process alive: no web page, no exit, so neither Docker nor NSSM restarted or reported it. Now:
  - **any failed start closes what it opened**, so the process ends (uvicorn's exit code 3), and a service manager sees the failure;
  - **a key that cannot open `secrets.enc` stops `openchart run` before the port opens** (exit code 2) with an explanation: put the old key back, or move `secrets.enc` aside and enter the tokens again;
  - **compared with a recovery mode** (start without the tokens and show the problem in the page): recovery keeps charts and history browsable and lets the tokens be entered again in the browser, but the server would then look healthy while it has no live prices and sends no alerts, which is the failure an alerting server must not hide. Stopping is loud everywhere (Docker shows "Restarting", the service log and NSSM's events repeat the reason, the health check fails). A missing secret store (no key at all, no credential manager) still starts as before and shows why in Settings: nothing is lost there.
- **`secrets.enc` and backup zips are created with mode 600** on Linux (Docker), from the start rather than changed afterwards; the image's umask made them 644. On Windows the data folder's ACL protects them, as before.
- **The "listening on 0.0.0.0 for other devices" warning was wrong in Docker**, where the server always listens on 0.0.0.0 inside the container and the published port decides who can connect. In a container it now says so instead. README: notes on Docker (setup code on the same PC, the key, bind mounts, memory).
- **Commands that write refuse while a server runs (found applying storage step 1 to the owner's data).** Only `purge` checked the data-folder lock; `openchart migrate` would have upgraded a database under a running server, and a second server only logged a warning. Now the commands that write (`migrate`, `backfill`, `rebuild-cache`, `repair`, `purge`, `reset-password` before it asks for the password, `secrets to-file`) hold the lock while they work and refuse with exit code 4 and "stop it first ... Nothing was changed"; the server refuses to start on a folder someone holds (a command or another server). Commands that only read (`stats`, `gaps`, `test-connection`, `secrets status`, `backup`) still run beside a server, but opening a database can upgrade it: with a server running and a migration pending they refuse instead (`Database.open(upgrade=False)`). `install-service.ps1` now stops a running service before it migrates the database and moves the tokens, which it did after them before.

## CI-1: the indicator worker (custom indicators, 2026-10-15)

Plan: `docs/ci1-plan.md`; what it protects: `docs/custom-indicators.md`.

- **Your files never run in the server.** The registry imports built-in files only; your files are sent to the worker as source text, which describes them and computes their values. A test proves it: a file that writes a marker when imported leaves none, and fails with "cannot open files" (line 1). Built-in indicators stay in the server (D5).
- **Separate namespaces.** A file of yours named `ema.py` no longer replaces the built-in EMA. `GET /api/indicators/{id}` takes `kind=builtin|custom`; without it a built-in wins. Until CI-3 gives the menu its sections, the M7 menu's flat list keeps the built-in, and the catalog's new `builtin` and `custom` lists carry both.
- **Files are read again when their modification time or size changes**, so no restart is needed. Three crashes, timeouts or memory limits in a row stop a definition until its file changes; its own exceptions are shown with their line, not counted.
- **Which worker** (`indicators/worker/isolation.py`): the service gets an AppContainer only when it runs the release build and the installer recorded `OPENCHART_SANDBOX_SID`; otherwise custom code does not run there (D7 a), and the service log says `indicator sandbox: unavailable: <reason>`. A source install gets the restricted token (Low integrity), falling back to a plain job-limited child. **A release build run by hand** (not the service; not in the plan's table) gets the restricted token at its own integrity, since PyInstaller cannot start at Low (finding 9): below the minimum, like a source install. Docker with compose gets the sidecar; without it, Linux and macOS, a child process.
- **Probes at every start** decide the level: the worker is probed before it is armed (environment, the database, `secrets.enc`, the key file, the data folder, the server process, a 127.0.0.1 port of its own, 192.0.2.1 for AppContainer and sidecar only, starting a program). For AppContainer and the sidecar any of these allowed means unavailable, never a fallback. Probe paths are absolute: a relative data folder made every probe say "absent" (found in the preview). The server log lists the results.
- **AppContainer details from CI-0:** `LOCALAPPDATA` in its environment, a private window station and desktop under SYSTEM. Its own folder: see the investigation below (the deny entry first added there did not hold and is gone; the folder is treated as writable, emptied at each start and capped).
- **The sidecar** serves each connection in a forked process (so `openchart sandbox check` works while the server holds its connection) and each request in a child with `RLIMIT_AS` = its size + the budget, `RLIMIT_CPU` and the deadline. It clears the image's `OPENCHART_*` from its environment.
- **`openchart settings set` writes beside a running server (owner's decision).** It is the one exception to "commands that write refuse while a server runs": only `indicator-editing` (auto, on, off) and `behind-proxy` (on, off), values validated; it refuses when the database needs upgrading; one row in its own transaction with a 2 s busy timeout, failing with "nothing was changed" rather than waiting; it never takes or releases the data-folder lock. Tested beside a real `openchart run` process holding the lock (where `migrate` still gets exit 4), with a database held by another writer, and against every settings-like HTTP route (none changes them). The server reads both on each request.
- **The D2 gate** (`api/editing.py`) is built and shown in the catalog (`editing`); CI-4's write endpoints will call `require_editing`. The rule is the request-origin rule recommended in the CI-0 report.
- **The installer** backs up an existing data folder first (`openchart backup`, stops if it fails; `-NoBackup` skips), grants the AppContainer read and execute on the release folder only (`openchart sandbox sid`), records the SID in the service's environment, and after the start waits for `indicator sandbox: ready (AppContainer)` or reports the reason; `-NoSandbox` removes the grant. **The uninstaller** runs the service once as `openchart sandbox remove` (the profile is SYSTEM's), removes the grant and prints the proof list. CI checks all of it as LocalSystem (`tools/ci/service-reinstall-test.ps1`).
- **Measured on the owner's PC** (restricted worker, source install): start 1.9 to 2.6 s; a script on 5,000 bars 1.9 ms round trip, 6.5 ms for SMA on 5,200 bars. The AppContainer and sidecar numbers are CI-0's (start 1.0 to 2.4 s, 3 to 18 ms round trip); CI repeats both.
- **Not verified on this PC:** the AppContainer as the user (it needs decoys in the Credential Manager and the registry: CI's Windows runner, `OPENCHART_TEST_APPCONTAINER=1`), the service as LocalSystem, Linux (process only, the sidecar's tests) and the compose sidecar. They run in CI on the next push.
- **The M7 menu** lists files that do not run with their reason and shows the isolation notice in one line; "restart the server" is gone from its footer.
- Removed: `tools/ci0_spike/` and its workflow; the spike report stays.
- **Owner's review of CI-1 (2026-10-15): the three decisions accepted.** (1) A release zip run by hand gets the restricted token at its own integrity and the "weaker isolation" notice; a test pins it, and the build's smoke test now requires the notice in the log. (2) The M7 menu lists files that do not run, with the notice. (3) Only crashes, timeouts and memory limits count toward the quarantine; a script's own exceptions are not counted, so they are kept instead: the same script, parameters and bars get the stored error without running again (the last 256), and a script that raised after more than half its deadline is not run again with those parameters for 30 s, whatever the bars (a forming bar changes them every second). Editing the file runs it at once. The per-job deadline applies to every job as before.
- **First CI run of CI-1 (found and fixed):** (1) on GitHub's Windows runner, which runs without an interactive window station like a service, the restricted worker died with `STATUS_DLL_INIT_FAILED` on the session's desktop (finding 7 again): a sandboxed worker started without an interactive window station now gets a window station and desktop of its own, as the AppContainer does under the service. (2) The sidecar could list `/data`: not the data volume (not mounted there) but the image's own empty folder; the image now makes `/data` the server user's only (mode 700) and the sidecar starts in `/`. A stricter setup, not a weaker probe. (3) The Windows test module imported `winreg` before skipping, which stopped collection on Linux.
- **No interactive window station (owner's decision, option A, discussed as "session 0").** GitHub's runner turned out to be session 2, but on a window station that is not interactive, like a service's; that is the test now (`interactive_window_station`, the same as .NET's `Environment.UserInteractive`). Outside a service, such a server (a CI runner) gets the job-limited process-only worker straight away, below the minimum with the "weaker isolation" notice; the restricted token cannot start a worker there even on a desktop of its own. Interactive sessions (a source install or a release zip run by hand on the PC) keep the restricted token. The smoke test (now `packaging/smoke-windows.ps1`, its own CI step) expects "process only" without an interactive window station and the restricted token otherwise, and the notice always. The tests of the restricted identity skip there; the job-limit and audit-hook tests run there with the plain worker.
- **A service never falls back (D7 a, proven).** A service install is LocalSystem, or any process with the installer's `OPENCHART_SANDBOX_SID`: it gets one launcher, the AppContainer, or none. Tests: the plan for a service holds only the AppContainer; a source checkout, a missing or mismatched SID give no launcher at all; when the AppContainer cannot start, no other identity is ever started, the catalog's Custom section and "Files that did not load" show the reason, values answer 503; an AppContainer or sidecar whose probes fail the minimum stops there too. No fallback existed before either; the SID condition is new (an installer-made service running under another account would otherwise have fallen back).
- **CI:** the service test depends only on the build, not on the smoke test. The AppContainer test's 127.0.0.1 probe aims at a real listener, checked reachable from outside the container first.
- **Finding 2 checked in CI:** the service run showed "write into its own folder: allowed", so the server's removal of that write did not take effect there. The CI service test now prints the notes line and requires that probe refused, and the AppContainer test applies the same removal and requires it too. Not yet explained; the next CI run says which note applied.
- **The AppContainer's own folder, investigated (owner's account, a throwaway profile, 2026-10-15).** `AC` carries an explicit full-control grant for the container's SID, and Windows gives it no size limit: 200 MB went in at once (the investigation's own cap). Files persist between workers (run 2 found the 207 files of run 1), so within one install a script can leave data for the next worker and, inside one worker, for every other script. Nothing outside is reachable through it: its package folder one level up is closed (list and write refused), a junction the user made inside it to an outside folder is refused both ways, it cannot make a symlink (privilege) or start `mklink` (job object). **The server's deny entry `(OI)(CI)(DENY)(W,D,DC)` for the container SID is written but does not hold**: after it, the worker still wrote, wrote in a subfolder and deleted. That is why the CI check failed, the same as LocalSystem (CI did not print that folder's ACL; its probe result and note match). Hence B1 (emptied at each start, capped while it runs); removing the container's own grant instead of adding a deny was not tried.
- **The own folder, decided (owner, after the investigation).** B1 stays (emptied before each start, measured every 0.5 s, killed past 50 MB). The deny step is removed: it never held, and its "writes removed" note claimed what was not true. The folder is now always treated as writable, from the first start; the note says it is writable, that all scripts of one worker share it and that what they write stays until the next worker start empties it. Files no longer persist between workers once B1 empties the folder; the investigation's "they persist" was without B1. Removing the container's own grant is left for a separate change. Investigation: `docs/investigations/appcontainer-own-folder.md`.
- **Second CI round:** no job hung (every job finished in 6 minutes or less). The A1 "other exit code" tests failed on GitHub's runner because a restricted process dies there with STATUS_DLL_INIT_FAILED before reaching the given exit code; they now use a plain-identity process labelled as the restricted launcher, so they test the decision itself everywhere.
- **The restricted-token mode (source install) is tested only on a developer PC.** In Windows CI its tests skip: a restricted process cannot start on GitHub's runner (it dies with STATUS_DLL_INIT_FAILED), so CI covers the job limits and the audit hook with the plain worker, and the restricted identity itself is covered by `tests/test_worker_windows.py` run on a PC with an interactive desktop (owner, after merging #17).

## CI-2 (a): the `ta` helpers, group 1 (averages and series), 2026-10-15

Plan: `docs/custom-indicators-design.md` 2.4. Code: `src/openchart/indicators/_ta.py` (replaces `_lib.py`; `ema` and `sma` are still exported from the package). Tests: `tests/test_ta.py`, written first against loop references written from the formulas, by-hand cases, a no-lookahead check for every helper at every cut, gaps, short inputs and the input's index.

- **`ema` and `rma` follow TradingView's documented recursion** (`sum := na(sum[1]) ? sma(src, length) : alpha * src + (1 - alpha) * sum[1]`) also at a gap in the input: NaN at the gap, then a new SMA seed once `length` values in a row are back. Before, `ema` carried its value across an interior gap (pandas' behaviour); leading NaN behave as before. Prices have no gaps, so the built-in EMA is unchanged; it matters when composing over an indicator that has them.
- **Output keeps the input's index.** The old `ema` returned a fresh 0..n index.
- **`hma`:** half length rounded down, root rounded to nearest, both at least 1. The Pine reference gives the formula with `length/2` and `round(sqrt(length))`; how a fractional `length/2` is cut is not stated there, so rounding down is my choice. `hma(x, 1)` is `x`. Tested against a straight line (exact for length 4 by hand).
- **`vwma(x, volume, n)` takes the volume explicitly** (no implicit volume series in a file). A window with zero volume gives NaN, not infinity.
- **`rising` / `falling` mean every step in the same direction:** `ta.rising(x, n)` is `x[0] > x[1] > ... > x[n]` (`n` steps, `n + 1` values), falling the mirror. **Checked against TradingView by the owner at n = 2**: it differs from "higher than all of the previous n values" on ordinary bars. Other values of n follow the same convention by inference, not by check. Tests pin it, including `[2, 1, 3]` at n = 2, which the other reading would call rising. (First built as "higher than all" from the reference's wording, then changed after this check.)
- **`crossover` and friends accept a number on either side**, give False at the first bar and wherever either side has a gap on the bar or the one before.
- **`barssince`/`valuewhen`** treat a gap in the condition as False; NaN before the first True. `valuewhen` returns `x` as it is on that bar, gap included.
- **`shift`/`change` refuse `n < 1`** with a message that names the future. Lengths must be whole numbers of at least 1 (not `True`, not `2.5`, not `"5"`), refused with a message.
- **Speed on 5,000 bars** (this PC, mean of 20): `sma`/`ema`/`rma` 0.14 to 0.18 ms; `wma(200)` 1.1 ms, `vwma(200)` 2.3 ms, `hma(200)` 2.0 ms; with length 2000: `wma` 10 ms, `hma` 17 ms; the series helpers 0.1 to 0.4 ms. `wma`, `vwma` and `hma` cost O(bars x length).

### CI-2 (a), group 2: oscillators and volatility

Tests first: `tests/test_ta_group2.py` (loop references from the formulas, by-hand cases, no-lookahead at every cut, gaps, short inputs, index, bad lengths).

- **RSI against Wilder's published worked example** (33 closes, 19 printed values to 2 decimals): all 19 agree. **The values are reproduced from the published example, not quoted from the source:** the sample is the one in StockCharts' RSI worksheet, I wrote the closes and values from memory (the worksheet itself was not read), and used them only after the independent formula loop reproduced all 19. Treat them as a regression check against the textbook method, not as a citation. **ATR has no published table in the tests**: StockCharts' ATR page links its rows only as a spreadsheet download, which was not fetched. ATR is tested against the loop reference and by hand (first range `high - low`, seed = average of the first `n` ranges).
- **`rsi` on a window with nothing moving (0 / 0) gives 100**, because the reference implementation tests `down == 0` first (100), then `up == 0` (0). I wrote this from my recollection of that implementation; the other form (`rma(up) / rma(down)`) would give a gap on a flat series, and agrees everywhere else. Worth one look on a flat series in TradingView; it hardly matters on forex.
- **`tr` always uses `high - low` for a bar without a previous close** (the first bar, or after a gap in the close): TradingView's `ta.atr` calls `ta.tr(true)`. There is no option for the other behaviour.
- **`stoch` is the raw %K** (`stoch(close, high, low, n)`, as in Pine); smoothing is `sma(stoch(...), 3)` by the author. A window where high equals low throughout is a gap rather than a division by zero.
- **`cci` uses the mean absolute deviation about the window's mean** (`ta.dev`'s definition) and a window with no deviation is a gap. "No deviation" allows for rounding (`mad <= 1e-12 * max(1, |mean|)`): without it, a window of `0.1, 0.1, 0.1` (whose mean rounds to `0.10000000000000002`) divides by `1e-17` and returns about `1e16`.
- **`stdev` is the population deviation** (divided by `n`); `bb` uses it with the `sma` as basis and `mult` defaulting to 2.0.
- **`macd(x, fast, slow, signal)`** returns `(macd, signal, hist)` with an EMA signal; the defaults are TradingView's 12, 26 and 9 (owner's request after review), and `bb`'s multiplier is 2.0, each pinned by a test. The signal's EMA seeds on the first `signal` MACD values.
- **`mom`/`roc`:** `roc` from a zero value is a gap, not infinity.
- **Tolerance:** the deviation-based helpers agree with their loop references to 1e-9 relative (prices near 2000 with a deviation of 0.5 lose a few digits to `x - mean` in both); everything else to 1e-12.
- **Speed on 5,000 bars:** `rsi` 1.0 ms, `tr` 0.5 ms, `atr(14)` 0.7 ms, `stoch(14)` 0.7 ms, `macd` 0.6 ms, `mom`/`roc` 0.1 to 0.3 ms; `cci`, `stdev` and `bb` 0.4 to 0.6 ms at length 20, 3 ms at 200 and 28 ms at 2000 (they cost bars x length).

### CI-2 (a), group 3: windows and pivots

Tests first: `tests/test_ta_group3.py` (loop references, by-hand cases, no-lookahead at every cut for every helper, gaps, short inputs, index, bad arguments), and mutation checks (wrong versions, each caught): a pivot reported on the pivot bar, ties going to the oldest bar, `highest` skipping the current bar, `cum` stopping at a gap, and after the TradingView check five wrong tie rules (left strict too, right loose too, the sides swapped, and the same two for `pivotlow`). One mutant, `k <= right` for `k < right`, survives because it is equivalent (that bar is skipped before the line).

- **Windows and gaps:** `highest`, `lowest`, `highestbars`, `lowestbars` and `sum` give a gap when any bar in the window is a gap, and a value again once the gap has left the window. Same rule as the averages. (Not checked against TradingView, which may skip gaps in `ta.highest`; a chart of prices has none.)
- **`highestbars` / `lowestbars` return Pine's offsets:** 0 for the current bar, -1 for the one before, down to `-(n - 1)`. **On a tie, the most recent bar: confirmed in TradingView** (owner's check with `highestbars(v, 3)`, 2026-10-15; `lowestbars` as its mirror, by inference). One by-hand test pins it; the random data has no ties.
- **`cum`:** the running total from the first bar; a gap is a gap on its own bar and the total carries on. It depends on where the history starts, so no warmup makes it agree with a longer history: the save-time warmup check (step c) will report it, correctly. Documented.
- **Pivot output convention:** the pivot's price on the bar where it is confirmed (`right` bars after the pivot), NaN everywhere else. A test proves there is no lookahead: for a peak known at bar 5, series cut at 1..5 bars show nothing, cuts of 6 or more show it at bar 5 and nowhere else; bars after the confirming one cannot remove it, and a higher bar among the `right` ones removes it.
- **Pivot ties, per side (owner's check in TradingView, 2026-10-15): bars to the left may equal the pivot, bars to the right must be strictly lower; a plateau of equal highs gives its last bar.** Checked in TradingView with `pivothigh(v, 1, 1)` on highs 1, 5, 5, 1 (the second 5 is a pivot, the first is not) and `pivothigh(v, 2, 2)` on 5, 1, 5, 1, 5 (no pivot, because of the equal high on the right). **Inferred, not checked:** windows of more than one bar on the left with some equal bars (`left` 2 with one or two equal bars, still a pivot; one higher bar, not), and `pivotlow` as the exact mirror image (equal lows allowed on the left, strictly higher on the right). The cases are in two tables in `tests/test_ta_group3.py` (`CHECKED_IN_TRADINGVIEW`, `INFERRED`), so what was checked is visible. This replaces the earlier single switch `_PIVOT_STRICT` (both sides strict, unconfirmed), which was wrong on the plateau.
- `left` and `right` must each be at least 1. A pivot needs all `left + right + 1` values present.
- **`sum` shadows the builtin inside `_ta.py`** (Pine's name; the module does not use the builtin, and ruff's A001 is silenced on that line).
- **Speed on 5,000 bars:** `highest`/`lowest` 0.2 ms at any length; at length 20, `highestbars` 0.4 ms, `sum` 0.2 ms, `pivothigh(20, 20)` 0.25 ms, `cum` 0.3 ms; at length 2000, `sum` 11 ms, `pivothigh(2000, 2000)` 17 ms, `highestbars` 30 ms (they cost bars x length).

## CI-2 (b): output kinds, `String`, `info`, schema and result validation (2026-10-16)

Tests first (`tests/test_indicator_schema.py`: one broken file and its message per rule; `test_indicator_results.py`; `test_custom_indicator_kinds.py` through the real worker; `test_untrusted_results.py` with a fake worker that lies). Written before the code, but not run red first.

- **One class per output kind** (`Line`, `Histogram`, `Area`, `Fill`, `Level`, `Marker`, `Background`); `Output` is an alias of `Line`, so M7 files load unchanged. Their JSON adds `kind` (and `style` for lines); the M7 fields stay. The only existing test that changed is the EMA's schema, which gained those two fields.
- **`Fill` and `Level` have no name, so they get ids `fill:1`, `level:1`, ...** (`:` is not allowed in an identifier, so they cannot clash with a name). They carry no values: `compute` returning one is an error.
- **Additions the design left open:** `Area` and `Level` take an optional `width` input, `Level` and `Fill` a `label`. `Marker` defaults to shape `circle`, position `above`. `Histogram`, `Marker`, `Background` and `Fill` have no width.
- **Names:** identifiers `[A-Za-z_][A-Za-z0-9_]{0,39}`, unique among inputs and, separately, among outputs (the design's own example has an input `fast` and an output `fast`). M7 only checked duplicates among inputs, so an M7 file with a name like `my-input` would now be refused. NAME, TITLE and a marker's `text` are held to the 60-character limit too (the design says "labels").
- **A file is rejected with `SchemaError`** naming the file first. The server rebuilds the schema the worker describes through the same rules, so a worker that lies about its schema is not believed. Messages keep the M7 wording where tests used it ("is not a color parameter").
- **What `compute` returns is checked twice.** In the process that ran the script (`Indicator.run`): a dict, exactly the declared value outputs (nothing else), one value per bar (the length is checked before anything is converted, so a million-value list costs nothing), numbers for lines, histograms and areas, real booleans for markers and backgrounds. Booleans travel to the server as 0.0/1.0. Then in the server (`Indicator.check_wire`) on what came back: float64 vectors of the right length, exactly the declared ids (an extra array is named and refused; a header that lies about its outputs is refused in the runner), 0 or 1 only for markers and backgrounds. A bad result is a `ScriptFailed("error")`: 422 naming the output, remembered like a script's own exception, never counted toward the quarantine, the worker not restarted.
- **A line that is NaN everywhere is allowed, not rejected.** It cannot be told from "not enough bars yet" (an SMA(200) on 100 bars), and rejecting it would put an error on short charts. Infinities in numbers are gaps. (The brief listed "NaN-only" among the hostile outputs: it is handled without harm, not refused. To confirm.)
- **`compute(bars, params, info)`:** `info` is passed only if `compute` declares a third positional argument (or `*args`). `Info` is a frozen dataclass with `instrument, display_name, decimals, timeframe, price`, built by the API from the instrument row; the worker checks its shape. It joins the key of the cached-error memory (the same script on another instrument is another run). `warmup(params)` is unchanged.
- **Long column names** `time, open, high, low, close, volume` are added beside `ts, o, h, l, c, v` for built-ins and custom files alike (a copy: the caller's frame is untouched).
- **Wire format of values** (`docs/api.md`): `values` keeps its shape for numeric outputs; markers and backgrounds are `hits: {name: [positions in t]}` (ascending); levels are `levels: {name: number}`, resolved with the request's parameters; `fill` has no values. A marker on 1 bar in 10 is a tenth of the numbers of a full array. Numeric lists are not compressed (5,000 numbers are about 60 KB of JSON).
- **Owner's answers (2026-10-16): A (NaN-only lines accepted), B (hits as index lists), C (`fill:1`/`level:1` ids), D (names must be identifiers: kept), E (the small additions), F (chart untouched until (d)).** For D the error now names the rule and the first offending character: `mine.py: input 'my-input': the name must be an identifier (letters, digits and _, starting with a letter or _, at most 40 characters); '-' at position 3 is not allowed` (also a leading digit, an empty name, a name over 40 characters, a trailing newline, non-text).
- **Hostile results do not count toward the quarantine.** A result that cannot be drawn is a `ScriptFailed("error")`, the same kind as a script's own exception, so the file is never stopped for it and keeps running for other requests. What bounds the cost: each job still has its deadline (5 s by default; a script that builds a huge answer is also under the worker's memory limit, and a frame over the protocol's cap is refused), the same bars and parameters are answered from the cache of errors without running again (256 entries), and a bad answer that came after more than half the deadline pauses that script and parameters for 30 s. The worker is not restarted by a bad answer, because it did nothing wrong as a process.
- **A marker (or background) is checked on the server only to be 0 or 1.** Booleans cross the protocol as float64 arrays, so the server cannot tell a real `True/False` from the numbers 0.0/1.0; only the process that ran the script can (`Indicator.run` refuses numbers, NaN and None there). The server's check is therefore: right length, float64, every value exactly 0 or 1 (NaN, infinities, 2 and 0.5 are refused). A compromised worker could send any pattern of 0/1 for a marker; that is the worst it can do, and it is bounded by the bar count.
- **Mutation check of the validation rules (same method as the `ta` groups):** 62 mutants, one rule broken in each (length check on both sides; type checks for lines, markers and text; undeclared, missing and ghost outputs; the 0/1 check; infinities; hits order and offset; value offset; level resolution; the answer's list of outputs; the server check skipped; quarantine counting; the schema trusted; both size limits; the identifier, uniqueness, Fill, Level, reference-type, min/max, choices, default, count, label-length and opacity rules; String limits; `info`; long names). 60 were killed at once. **Two survived and got tests:** hits counted from the start of the warmup instead of the first requested bar (the earlier test used an indicator without warmup, so both offsets were equal; now `test_hits_and_values_start_at_the_first_bar_asked_for_not_at_the_warmup`), and an opacity of exactly 1 (`tests/test_indicator_boundaries.py`). Rerun: all 62 killed. The script is not kept in the repository; the list is above.

- **Old browsers:** the M7 chart reads every output as a line (`params[o.color]`, `params[o.width]`, `values[o.name]`). A file with the new kinds, listed in the menu before step (d), would draw a histogram as a line and mishandle `fill` and `level` (no width, no values). Nothing in the browser was changed in this step.

## Tests that depended on the time of day (2026-10-16)

`test_websocket_higher_timeframe_starts_from_history` failed on all four Windows jobs of PR #18 (a docs-only change; the same branch's Linux jobs passed). The cause was the clock, not the change: `conftest.py` built three days of history up to the minute it was imported (`NOW = int(time.time()) // 60 * 60`), the test pushed a tick stamped with the clock of the moment it ran, and the jobs started pytest at 20:59:57 and 21:00:16 UTC, either side of the 17:00 New York break. The tick began a new trading day with none of the stored history in it, so the daily bar held only the tick (`1 > 1`). Linux jobs started 20 to 40 seconds earlier and most likely reached the test before 21:00 (inferred from the step timestamps). Reproduced for Windows with a fake clock: collected 20:59:30 and run 21:00:30 fails with `1 > 1`; collected after 21:01:00, or run before 21:00:00, passes.

- **Fix: one fixed timeline for the test process** (`tests/clock.py`, installed first thing in `conftest.py`). It starts at Wednesday 2026-10-14 15:00 UTC (11:00 New York, mid session) and runs forward at the normal rate. It replaces `time.time` and, for the `openchart` modules, `datetime.now`/`utcnow`, so the tests and the server (which reads both: live series, the feed, backfill, the OANDA paging) agree. Waits (`monotonic`, `sleep`, `fail_after`) are untouched. The failing test also pushes its tick at `NOW + 30`, the same trading day as the history by construction.
- **Wider than the one test, because the same clock decided more.** A search over every test and fixture found 55 uses of the real clock in 20 files, and instead of judging each by reading, the whole suite was run on `main` under a fake clock at nine moments: mid-session (506 passed), 20:59:30 UTC summer (one timing failure, see below), 21:59:30 UTC winter, Friday 20:58, Saturday noon, Sunday 21:58, the night US clocks go back, 23:58 on 31 December (all passed except the following). **On a Saturday, and on a Sunday before the market opens, 7 tests failed:** the six drawings tests (`test_create_list_update_delete`, `test_update_checks_point_count`, `test_every_open_browser_receives_changes`, `test_delete_all_for_an_instrument`, `test_drawings_survive_a_restart`, `test_an_anchor_beyond_the_last_bar_is_one_update_not_a_new_drawing`) and `test_a_cross_during_a_freeze_fires_late_with_a_freeze_message`, because their points were "a few hours ago", inside the weekly closure, and the API refused them; at Sunday 21:58 also `test_eurusd_4h_bars_follow_its_own_hours`. So the suite would have failed on any CI run on a weekend. All are fixed by the timeline, with no test changed.
- **Regression test** (`tests/test_clock_independence.py`, with the plugin `tests/fake_clock.py`): four of those tests run in a child pytest whose real clock is moved to seven moments (summer and winter break, just after the break, the Friday close, Saturday, Sunday before the open, the DST night), with a minute passing between collection and the tests as on a slow runner. All pass. Run against the old `conftest.py`/`test_api.py` the same test fails at all seven. A control (`OPENCHART_TEST_REAL_CLOCK=1` leaves the real clock) keeps the drawings test failing on a Saturday, so the test cannot silently stop seeing the problem.
- **Not clock dependence, seen once:** `test_worker_runner.py::test_a_job_past_its_deadline_is_killed_and_a_new_worker_starts` took 2.96 s against its 2.5 s limit (a `perf_counter` limit on a worker restart) in one run while another job was loading the machine. Not changed; worth watching.
- **Not covered:** the Monday 00:00 UTC moment was cut short, and the sweep of the break across every position in the suite was not run; the remaining uses of `time.time()` in the tests (about 40) now read the timeline and need no change. `tests/test_live_oanda.py` uses `datetime.now` and is only run with the owner's go.

## CI-2 (c): the save-time lookahead and warmup checks (2026-10-16)

Design approved by the owner with decisions A and B and five additions. Tests first (`tests/test_lookahead_checks.py`: the rules, on scripts compiled as the worker compiles them; `test_check_scheduling.py`: priority, keying, quarantine, stagger, with the fake worker; `test_checks_api.py`: the API and the real worker). Run red against a stub that planned and found nothing and a runner without priorities: 79 failed and 4 errors, each on its own assertion. Five passed against the stub because they assert that something is *not* flagged (the three known limits, gap equals gap, built-ins not checked); the mutation check below is what shows they bite.

- **When (A):** there is no save until CI-4, so a "save" is a new version of the file (a new hash). The check runs on the bars of the first chart request for that version **and settings**, with that request's `params` and `info`, keyed by (version, settings hash), for at most 5 settings per version. A sixth is answered "not checked for these settings" and the five results are kept. A new version starts over and drops the old one's waiting checks.
- **Where (B): the script runs only in the worker; the comparison runs in the server**, on the arrays the worker sends back (checked by `check_wire` like any answer). Reason: a verdict computed in the worker would be one more answer from code the server does not control. Comparing returned arrays is plain numpy on data; a lying worker can only cause a wrong or missing warning, never run anything in the server. The full run is the chart request's own answer, so a check costs two more runs, not three.
- **Two worker jobs at the lowest priority.** The runner's queue (an `asyncio.Lock` until now) has two priorities: a check's job starts only when no job runs and no chart request waits, so a chart waits for at most one check job (tested with three queued check jobs and two chart requests: the charts go second and third). One check at a time, 1 s apart, none within 10 s of a worker becoming ready (`runner.ready_at`, also after a restart; while a worker is restarting the checks wait for it).
- **The lookahead run:** the bars without the last K = min(50, n // 4) (none below 8 bars); every shared bar is compared, from bar 0, because both runs start on the same bar. The declared warmup plays no part. **A forward shift of any distance is caught**, also past K: `shift(-60)` with K = 50 leaves bars n-110 to n-60 different (tested).
- **The warmup run:** the bars from `start = min(max(warmup, 50), n - warmup - max(20, n // 4))` (skipped below 1), compared from `start + warmup`, where the offset run has exactly its declared warmup behind it. The warning gives `needed` = (the last differing bar's history in the offset run) + 1, a lower bound for this data and tolerance; if the last bar still differs: "no warmup can fix that" (`ta.cum`, by design).
- **Equality:** a gap (NaN, ±inf) equals a gap, a gap differs from a number; numbers within `tolerance x the output's largest finite absolute value in the full run` (0 when it has none, so then exact); tolerance 1e-9 for lookahead (summation-order or FFT noise; causal code on an identical prefix is bit-identical in practice), 1e-7 for warmup (the helpers document 1e-8 agreement at their warmup). Markers and backgrounds exactly.
- **Owner's point 3, a marker that flips on a near-tie in the warmup run:** the offset run sums in another order, so a line can agree within 1e-7 while `line > k` flips on a bar where the two are equal to within rounding. Rule: in the warmup check, a true/false output that differs **only where every line of the indicator agrees within tolerance** is a `note` ("most likely a near-tie"), not a warning. Tested with a 5-bar sum built from `cumsum` and a threshold placed between the two runs' values of one bar. Limits: a file with only markers cannot be told apart, so it stays a warning whose message mentions near-ties (tested); a marker computed from an internal series that is not an output, with a too-short warmup, while the declared lines agree, is downgraded to a note (a missed warning, accepted). In the lookahead check booleans stay exact: both runs share the prefix, so a near-tie cannot flip there (tested with the same script).
- **Time budget:** a run starts only if the last run of the script took at most 80% of the deadline (`runner.last_job_s`: the exchange with the worker only, not the queue and not a worker start, which an earlier version counted and which skipped checks after every restart, found by the timeout test). Otherwise it is `skipped` with the reason. A run that times out restarts the worker like any job, is reported as `timeout`, **never counted toward the quarantine** (the check calls the runner directly, not `CustomIndicators.compute`), and the warmup run is then skipped. A run that raises on fewer bars is `failed` with the script's message, not kept in the error cache.
- **Built-ins are not checked** (only `CustomIndicators.compute` queues checks); their answer has no `checks`.
- **Wire format** (`docs/api.md`): `checks` in the values answer and a list in each `custom` entry of the catalog; states `pending|ok|warned|skipped|timeout|failed|not_checked`; findings `{check, level, output, t, full, other, message}` (+ `needed` for warmup warnings), one per output and check, at the first differing bar. Warnings are logged once per check.
- **Known limits, pinned by tests that assert they are NOT flagged:** `bfill` inside the warmup, a forward read that removing the last bars does not change (`c / c.max()` with an earlier maximum), a bar-count branch both runs take the same way. Also seen while writing the tests: **a true/false output is only seen to look ahead where it flips** (`c.shift(-1) > c` passes when the removed close is lower). The docs say the checks find some lookahead, not all, and never call a passing check safe.
- **Found by the checks in our own docs:** `barssince` needs its condition to have been True once inside the warmup (docs/indicators.md said "none": fixed, with a test). `tr` with no warmup is not flagged on the test data only because each bar opens at the previous close; on real data with gaps its first bar is `high - low`, so 1 bar of warmup is right (the helper test uses 1). The CI-1 test file (`GOOD`, an SMA(5) with no `warmup`) is told it needs 4 bars, which is right. Every other helper passes both checks with the warmup docs/indicators.md gives it, and the design's SMA cross, the every-kind file of CI-2 (b) with `warmup` 5, and the built-in EMA file pass.
- **Mutation check of the checker** (same method as CI-2 a and b; the script is not kept, the list is here): 42 mutants, one rule broken in each, run against a copy of `src/` after a baseline run of the new tests on the unbroken copy:
  - K = 0; K = half; comparing one bar fewer; comparing from bar 1; reporting the last differing bar;
  - warmup compared inside the offset run's warmup; NaN equal to a number; infinity as a number;
  - tolerances 0 and 1e-3 (each check); the scale from the shortened run; the warmup scale from the compared bars only; markers given the line tolerance;
  - `needed` off by one; `cum` not "never agrees"; both skip guards removed; the offset without its minimum of 50;
  - the near-tie rule removed, applied without lines, and with lines always agreeing; no time budget; budget share 1.0;
  - a check timeout counted toward the quarantine (the owner's mutant); the warmup run started after a crash or timeout;
  - checked again on every request; six settings; checks run with the defaults or without `info`; checks at normal priority;
  - no stagger; no gap; not waiting for a restarting worker; run time counting the queue and the worker start; `ready_at` not set on a respawn;
  - the runner ignoring low priority; low jobs not yielding to waiting charts;
  - built-ins checked; no `checks` in the answer; the declared warmup not passed.

  **The first run was wrong, and showed it:** all mutants from the 14th on were "killed" by the same test. That test had been changed (the output's largest value moved into the removed bars) and **failed on the unmutated code: the comparison took its scale from the compared bars, not from the whole full run** as designed. Fixed in the code, and the script now refuses to run unless the baseline passes. Second run: 33 of 42 killed. **Survivors and what was done:** the first vs last differing bar (`shift(-1)` differs on one bar only: a `shift(-3)` test added); the warmup scale (test added); checks at normal priority (a test that the two runs are `low` jobs of n − 50 bars each); not waiting for a restarting worker and `ready_at` on a respawn (the restart test compared against `ready_at` itself; rewritten to check that a check stays pending while the worker is down and pauses again after it is back); the warmup run after a timeout (a timed-out run always exceeds 80% of the deadline, so the budget skips it anyway; the rule matters after a crash: a test with a script that ends the worker on fewer bars). Third run: **41 of 42 killed**. The survivor, "markers given the line tolerance", is equivalent: markers are 0/1, so they differ by 1, more than any tolerance x a scale of at most 1. The explicit exact rule is kept for readability.
- **Red, then green:** the 79 failures above were against the stub. The five tests added after the mutation run were each shown to fail on their mutant, not against a stub.
- **Owner's review: no test may depend on the machine's speed** (CI's Windows runners are slower than the owner's PC). The pauses (1 s apart, 10 s after a start) and the 80% budget now run on an injected clock: `IndicatorRunner(clock=...)` stamps `ready_at` and `last_job_s` with it, and the check loop reads `runner.clock` and sleeps with `CustomIndicators.sleep`. The tests use a `ManualClock` that moves only when the test moves it, so the real defaults (10 s, 1 s) are tested as they are, in milliseconds. The order of jobs is held by a gate in the fake worker (`GATE <folder>`: it writes `started-<n>` and waits for a file `go`) instead of a 300 ms pause, and the budget is tested by charging each exchange a set time on the clock (2.25 s of 2.5 s: skipped; 2.0 s, exactly 80%: runs). The old test's "under 1.2 s" bound is replaced by the order itself, which is the property: only `check0` finished before the two charts. Only the deadline tests use real time, because the deadline is real; their scripts loop forever or end the worker, so a slow machine cannot change the outcome, and their deadline is 2 s so the chart's own request is never near it.
- **Found while doing that: a worker that died between jobs counted as ready**, so a waiting check would have started it again and run without the pause. `runner.up` (ready, and the process alive) is what the check loop now waits for; tested by killing the worker under a waiting check.
- **A flaky test, caught by the mutation tool:** a mutant that cannot touch the runner was "killed" by `test_the_run_time_counts_neither_the_queue_nor_a_worker_start`. The test killed the worker and sent the next job at once, so the job could reach the dying process (a crash) instead of a new one. It now waits until the worker is gone, as the other kill test does; 30 runs in a row pass. The mutation script is now `tools/mutate_checks.py` (44 mutants, with the multi-edit ones: timing that starts before the worker start, a dead worker counted as up, the budget's `<=` made `<`); it refuses to run unless the tests pass on the unbroken copy, and exits 1 on a survivor, 2 on a mutant that no longer applies. Result: 43 killed, 1 equivalent (markers given the line tolerance).
- **Stress runs (owner's review):** the new tests 20 times in a row, the 20th with a busy loop on each of the 12 cores. Runs 1 to 19 passed. **Run 20 failed in `test_checks_api.py`'s fixture: the real worker did not start within the server's 60 s.** Measured alone: the restricted worker starts in 1.7 s with 0 or 6 cores busy, and does not start within 60 s with all 12 busy, also when the busy loops run at the worker's own below-normal priority. So it is not the scheduling priority; most likely the job object's hard CPU cap (50% of one core) once every core is saturated (inferred, not proven). **This is CI-1's worker start, not the checks**, and CI runners do not run other loads; it is reported to the owner as a separate item. The fix here: `test_checks_api.py` did not need the real worker. What it proves (the format, the request's own params and `info` in the check's runs, built-ins skipped) is shown with the fake worker, which now refuses (`STRICT`) any request but the one colour on XAU_USD 5m. Real scripts are checked in `test_lookahead_checks.py`, in-process. Run again after the change: 20 of 20 passed, the 20th with all 12 cores busy (93 tests, 277 s instead of 40 s). The mutation tool then caught what the switch had lost: "the declared warmup not passed" survived, because the fake worker declared no warmup (the default). It now declares one (`WARMUP <n>`), and the API test checks 320 bars with a declared warmup of 20; the mutant is killed again.

## Review of CI-2 (b) to (d) as someone else's work (2026-10-16)

Method: read the code of (b) (schema and result validation), (c) (the checks and their scheduling)
and (d) (the chart), then tried to break it: hostile and degenerate requests against the real routes
and the real worker, the four documentation examples on empty, one-bar and weekly windows, and the
chart in a real browser (edit a file while it is open, a stale layout, 16 outputs and 26 inputs in
one legend, a marker on every bar, NaN-only outputs, marker-only panes, 25 rounds of hide/show on 6
indicators, remove everything, a weekly chart of four bars). What held up: the schema and result
checks on every hostile return value tried (13 shapes), warmup and window arithmetic on 0, 1, 2 and 3
bars, every constant the docs quote (checked against the code), `print()` going to the log, 15,000
markers without a long task, no leftover pane after removal.

**Fixed, each on its own branch with the test first (frontend and docs):**

- The chart did not redraw an **edited indicator file** (only the last two bars changed, so a line
  mixed two versions of the code), did not show a script that then raised, and never retried one that
  had failed: a full load on a new `checks.version`, on a server error, and every 5 s while in error
  (`refresh.ts`, 7 tests; browser-checked).
- A **layout's stored parameters** that the file no longer has (or has with another type or range) made
  every request a 400 and the indicator "Could not compute" for good: `reconcileParams` (8 tests).
- **Legend rows** with many outputs could not wrap (CSS; no automated test possible here).
- **`docs/api.md`** did not describe 5 of the 8 keys of the catalog answer or the `kind` parameter and key
  (a docs-consistency test, red before).
- **`docs/roadmap.md`** listed merged steps as next.
- The legend's `∅` for the live bar was real and is fixed on its own branch (see the section above on
  indicators and the forming bar), as asked.

**Backend, not fixed (for the owner; each has a repro):**

1. `GET /api/indicators/ema?...&params={"length": Infinity}` (also `-Infinity`, `1e400`) is a **500**.
   `Param.check` catches only `ValueError`, and `int(float("inf"))` raises `OverflowError`; Python's
   JSON parser accepts `Infinity`. Any `Int` or `Width` input of any indicator. Suggested: test
   `math.isfinite` before `int()` (the `float` branch already does), or catch `OverflowError`. Also
   `NaN` answers "cannot convert float NaN to integer" instead of "must be a whole number".
2. **A check can be spent on a tiny request.** `_want_check` keys the save-time check by (file version,
   params) and the first values request for that key decides its bars. In the browser, after a file
   edit the first request is the once-a-second two-bar refresh: lookahead and warmup are then "skipped:
   too few bars" for that version and those params, and every later full request returns that same
   stale result (`bars: 0`; reproduced with `from=0&to=1`, then `limit=600`). A script that peeks
   (`shift(-3)`) gets no warning. Suggested: keep the result with the most bars, i.e. queue the check
   again when a later request has more bars than the stored one and the stored one was skipped for too
   few (the key is still bounded by `CHECK_SETTINGS`).
3. **`from` or `to` beyond 64 bits is a 500** on `/api/indicators/{id}` and on `/api/candles` (SQLite
   cannot bind the integer). Suggested: clamp, or 400. Authenticated route; low severity.
4. A file's change stamp is `(mtime_ns, size)`: two edits of the same size within the file system's
   timestamp resolution (FAT/exFAT 2 s) would not be seen. NTFS and ext4 are fine. Low severity; a
   content hash on every catalog read would fix it at the cost of reading each file.

5. **A layout with a custom indicator cannot be saved today.** `POST /api/layouts` with an entry for a file of
   yours (`all_kinds`) answers 400 `no indicator 'all_kinds'`: `routes_layouts.py` looks entries up in the
   built-in registry only. The design plans it for CI-3 (layout entries gain `kind`, "old entries are read as
   built-in"), but since CI-1 a custom indicator can be put on a chart, so "Save layout" fails for any chart
   that has one (the chart itself keeps it in `localStorage`, which is why nothing else shows). Minimal fix
   until CI-3: accept an id that is a custom file, and validate its parameters against that file's schema
   (the same `normalize` the values route uses). Test: save a layout with a custom entry, load it.

**Seen, not changed (frontend):**

- When several outputs share one colour parameter, the other swatches in the open settings dialog keep the
  old colour until it is reopened (the chart updates at once).
- Own-pane indicators are formatted with the instrument's decimals: `applyFormat` sets one chart-wide
  `priceFormatter`, so an RSI reads `70.000` on gold and would read `70.00000` on EUR/USD (axis, level labels and
  legend). A per-pane format means dropping that chart-wide formatter, which also formats the drawings' labels:
  not done unattended.
- The chart assumes `hits` ascending and without repeats, as the server guarantees; a wrong server would
  hand the library unsorted markers.
- Refreshes run only while ticks arrive, so with the market closed an edit is noticed at the next history
  load.
- Two marker outputs on the same bar and side overlap (the library does not stack them).
- Every load of older history refetches every indicator in full (the M7 design; CI-5).

## The worker's start under full CPU load (2026-10-16)

Found in CI-2 testing: with every core busy, the isolated worker did not say hello within
the server's 60 s. Measured in `docs/investigations/worker-start-under-load.md` (idle and
under load, each factor on its own, repeated).

- **Cause:** the worker was created at below-normal priority. Next to as many normal-priority
  busy loops as cores it got 0.1 to 0.4 s of CPU in 75 to 150 s (importing numpy and pandas
  takes 1.1 to 1.6 s of CPU). Normal priority alone brings the loaded start to about 7 s.
  **Not the cause:** the 50% CPU cap (it doubles the idle start, 1.5 s to 3 s, and does nothing
  under load), the thread-limit variables, the AppContainer, the restricted token, the job
  setup (each under 100 ms). This corrects the inference written under "Stress runs" in
  CI-2 (c) above ("most likely the job object's hard CPU cap"): it was the priority.
- **Decided (owner):**
  1. **Done: start at normal priority, lower it from the server after the hello.** The
     process handle `CreateProcess` returned is used; the class is read back. Anything but
     below normal kills the worker (fail closed) and the Custom section shows why; a respawn
     follows the same path. Nothing (no script source, not even the probes or the arming) is
     sent before the hello and the lowering. The log says `priority lowered to below normal`
     with the pid, and a failed start says how much CPU the worker had used. The worker runs
     user code only after it is lowered, so isolation is unchanged: priority is not a
     boundary (the token, container, job limits and probes are).
  2. **Considered, deferred: lift the CPU cap during the start and restore it after the
     hello.** Benefit measured: about 1.5 s at idle (3.2 s to 1.5 s); nothing under load.
     Risk: a second job-object change while the worker runs, for a gain only in an
     occasional respawn. Not done.
  3. **Done: the start deadline is 90 s** (was 60). 1 to 7 s measured with normal priority;
     the deadline only has to cover the slow machine, not hide starvation (a start with
     little CPU used is now told from a hang in the log).
  4. **Rejected: lazy imports.** pandas is about 80% of the import time, but `compute`
     builds a `DataFrame`, so the cost moves to the first request and can break its 5 s
     deadline; it does not help under load.
  5. **Rejected: prewarming a spare worker.** The first worker already starts at server
     start and a respawn follows a kill; a spare doubles the memory and does nothing for
     the first start.
- **Seen, not changed:**
  - On a machine whose cores are all busy with normal-priority work, a worker that runs
    below normal gets CPU late: requests were answered in 10 to 20 ms (the pipe wakes it),
    but a graceful exit took about 40 s and a timed sleep woke up about 40 s late. A script
    that needs real CPU would then run into the 5 s job deadline. That is the design's
    "yield to everything else" at its extreme, and the quarantine counts such timeouts.
    Server shutdown under full load can wait for the worker's exit for as long (the tests
    kill the worker instead).
  - The earlier note that the worker also failed to start with busy loops at its own
    below-normal priority was not reproduced here (those runs used normal-priority loops).

## CI-2 (d): drawing the new output kinds (2026-10-16)

Frontend only, plus two demo tools. Nothing in `src/openchart` changed. The mapping from the
answer to what is drawn is in `frontend/src/chart/indicatorOutputs.ts` (no DOM, no chart, all
unit-tested), the two primitives in `indicatorPrimitives.ts`, the wiring in `indicators.ts`.

- **What each kind becomes.** `line`, `histogram`, `area`: a series of that type (line style
  from the catalog, histogram `base`, area shaded 0.28 to 0.02 of its colour). `fill`: a primitive
  painting the band between two value outputs, behind the series. `level`: a price line.
  `marker`: the library's markers. `background`: a primitive of full-height columns, behind the
  series, adjacent columns merged into one rectangle. Colours always come from the instance's
  params through the names in the catalog; a name that is missing or not `#rrggbb` is grey.
  Opacity comes from the catalog (clamped to 0 to 1).
- **One host series per indicator.** Fills, levels, markers and backgrounds hang on the
  indicator's first value output; an indicator with none (markers only, say) gets a hidden
  line series holding the closes. So they move with it between panes and go when it goes. On the
  price pane the hidden series does not take part in autoscale; in a pane of its own the closes
  give the pane a scale.
- **Markers use prices, not bar positions.** Above is `atPriceTop` at the bar's high, below
  `atPriceBottom` at its low, on `atPriceMiddle` at its close. On the price pane the bar is what
  the price series shows (the Heikin Ashi bar for that chart type, the close alone for a line or
  area chart), and the markers are drawn again when the chart type changes. In a pane of its own
  the price is the indicator's value on that bar (first value output that has one). Two marker
  outputs on the same bar and side overlap; the library does not stack them.
- **Hits stay sparse.** `hits` become a sorted list of bar start times (`t[i]`), kept as
  that; positions outside `t` are ignored. A live refresh replaces the hits inside the two bars it
  fetched and leaves the rest.
- **Levels** are drawn at the number in `levels` (already resolved by the server for the request);
  if the answer has none, from the catalog's number or the input's value. The axis label follows
  "Labels on price scale"; the level's `label` is not shown anywhere yet.
- **Lines break at gaps, like fills (owner's answer, 2026-10-16).** The library joins a line
  across points that have no value (tried: Lightweight Charts 5.2.1 draws straight across
  whitespace points), so a gap is drawn by giving the first value after it a transparent colour:
  the segment leading to a point takes that point's colour. Lines and areas break that way
  (an area's line and both fills); columns have nothing to join. Nothing is marked before the
  first value (warmup), after the last, or on the last point (a transparent colour would hide
  its price label, so a gap right before the live bar is bridged until it has a successor). Every
  built-in and any M7 file whose only nulls are the warmup produce exactly the data the M7 chart
  did (a test compares 200 random series of that shape with the M7 rule). A file with gaps in
  the middle now draws them where M7 joined them. The once-a-second refresh sends the last bars
  as a full load would, so a gap that ends there is kept; it never turns a held value into a gap.
- **Hidden, removed, changed, failed.** Hiding turns off the series, the markers, the price
  lines and both primitives. Removing detaches all of them and removes the series. A settings
  change re-applies colours, widths, styles and level lines for every kind. An indicator whose
  request fails clears every series, hit and level and shows "Could not compute" in the legend,
  as before.
- **A bug found in testing, fixed:** while an indicator was hidden, the once-a-second
  refresh still ran and built the fill and background data as empty ("not shown, so nothing to
  draw"); showing it again only switched the primitives on, so the band and tint were missing until
  the next full reload (seen once in three tries, because the hide had to outlast a refresh). The
  data is now built whether or not the indicator is shown (`buildBands`, tested), and the
  visible flag decides. Checked in the browser with a hide of 4 s.
- **A second one, caught by an M7-style file in the browser:** a file with no `warmup` gets
  `null` from a two-bar refresh. The M7 chart skipped a null in a refresh (the value from the full
  load stayed); my first version stored it and blanked the legend. A null never overwrites a held
  value again (`tailPoints`, tested). The same file's markers in the last two bars do disappear after
  the first refresh (the refreshed window answers "off"); that is a file the save-time warmup check
  flags, so it is left.
- **Settings dialog, minimal change beyond the brief.** The Style tab assumed every output has a
  width parameter; histograms, fills, markers and backgrounds have none, and level and area may not.
  It now shows a width control only where there is one. A `String` input (step (b)) had no
  control and fell through to a number box: it is now a text field with the `max_length` check.
  Nothing else in the dialog changed. Seen, not changed: when several outputs share one colour
  parameter, the other swatches keep the old colour in the open dialog until it is reopened (the
  chart updates at once).
- **Tests** (`indicatorOutputs.test.ts`, `indicatorPrimitives.test.ts`, 62 more frontend tests,
  113 in all): colour lookup, opacity, line styles, hits to marker times and placements, levels,
  fill and background inputs, the refresh rules, and a compatibility block: the built-in EMA and
  an M7-format file produce exactly the series options and data the M7 chart gave (a solid line
  sends no `lineStyle`). Mutation check as in the earlier steps: 25 mutants, one rule broken in
  each (index range and offset of hits, kind default, colour validation, opacity clamp, style codes,
  gap handling in fills and lines, window replacement, marker prices and shapes, text, level
  source, null in refreshes, area and histogram options, run splitting and merging, column
  edges); all 25 killed. The layer itself (`indicators.ts`) has no DOM test: the frontend tests run
  in node. It was checked in a real browser instead, below.
- **Checked in Chromium against a fake broker** (`tools/demo_output_kinds.py`, scratch data
  folder, no OANDA, no Telegram): every kind in both themes, a live theme switch, chart type and
  timeframe switches, hide and show, settings changes and cancel, removal (no pane, line, marker or
  tint left), an indicator that raises, an indicator hidden from the start, scrolling back eleven
  days (loaded in pages of 5,000), live refreshes about once a second per indicator.
- **Speed** (headless Chromium, software rendering, so absolute figures are pessimistic; mean
  frame time while dragging, 60 Hz is 16.7 ms):
  - normal zoom, 15,000+ bars held, three heavy demo indicators (11 value series, plus
    markers, fills, tints and levels): 18.2 ms (none: 16.7, EMA alone: 16.8);
  - zoomed all the way out (about 2,000 bars across, the library's limit): the same three 45 ms;
    a stand-in with lines, a histogram and an area but none of the extras 28 ms; four plain
    three-line panes 29 ms. Each extra kind costs about 0 to 2 ms per frame there. Merging adjacent background columns did not change it
    and stayed in (fewer rectangles for long runs);
  - a reload at 15,000 bars: about 77 ms in the library's `setData` for five series, about 11 ms
    for markers, levels and bands. Every load of older history refetches every indicator for all
    bars held (33 full fetches per indicator while scrolling back eleven days): the M7 design,
    which CI-5 is for. Not changed here.
- **The legend's `∅` for the live bar is real, not the demo's fake broker (checked by reading the
  code and watching the clock, no change in this step).** `CandleBuilder` writes an M1 bar to the
  database only when it closes; the forming minute lives in memory and on the websocket. `GET
  /api/candles` and so the indicator values come from stored M1, so a bar whose first minute
  has not closed is missing: for up to a minute after a 5m bar starts, and on the 1m chart the
  forming bar is never there (15:30:07, 15:30:27 and 15:30:47 all ended at the 15:29 bar). It
  was the same before CI-2 (M7 code, same server). The fix is on its own branch, see the PR.

## Indicators see the forming bar (2026-10-16)

Found in review of CI-2 (d): the legend showed no value for the live bar for up to a minute after a
5m bar started, and on the 1m chart for every live bar. Not the demo's fake broker: `CandleBuilder`
writes an M1 bar to the database only when it closes (the forming minute is in memory and on the
websocket), and both `GET /api/candles` and the indicator values read stored M1. Measured on the
demo (1m, 15:30:07, :27 and :47): both answers ended at the 15:29 bar. The same on `main` before
CI-2.

- **Fix:** `SeriesReader.candles(..., forming=True)` folds the builder's forming M1 bar (a copy,
  `CandleBuilder.forming_m1`) into the bars: into the last bar if it is the bar of that period,
  else appended (the first minute of a period, the 1m chart). Only the indicator route asks for it.
  `/api/candles` is unchanged: the chart merges websocket updates into the bar it has, and a bar
  that changes under a reload is a different change. To decide later whether it should follow.
- **Not applied** when the window ends before the forming minute (`to`), when it starts after
  that minute's bar, for sub-minute timeframes (built from ticks, which are buffered live), or if the
  builder's bar is older than the last stored bar (a stale builder must not put bars out of order).
  A full page (`from` and `limit`) drops the appended bar like any other that does not fit.
- **Consequence for the values:** the last value now moves with each tick (a bar that has not
  closed), like the last bar of the chart. The cache of results keys on the bars, so a changed
  last bar is a new computation, as it already was for a forming higher-timeframe bar.
- Tests: `tests/test_forming_bar.py` (19). Mutation check: 9 mutants of the fold, two survived the first
  version of the tests (a stale bar, and a high above all stored highs) and got tests; then all killed.

## CI-2 (e): the guide, the examples and the Pine sheet (2026-10-16)

`docs/custom-indicators.md` now starts with how to write a file: where to start, the four
examples of the design (SMA cross, RSI with bands, Bollinger-style fill, an engulfing-candle
marker), the output kinds, a cheat sheet from Pine Script and what has no equivalent, then the
save-time checks, the worker, isolation by install, the gate on browser editing and what none of
it protects against (the last four were written in CI-1 and CI-2 (c) and are only moved and
renamed).

- **The examples are files** in `docs/examples/indicators/`, and the page shows them verbatim
  (`<!-- example: name.py -->` before each block). `tests/test_doc_examples.py` runs each against the
  real worker on a seeded random walk (the synthetic bars of the other tests close up every bar, so
  an engulfing pattern never occurs): the file loads, every line has values, every marker and
  background fires and not on every bar, both save-time checks say `ok` with no findings, and the
  values and hits on every bar are the same whichever later bars exist (4 cut points each).
  Controls so the tests can fail: a variant of the SMA cross with `shift(-3)` is warned by the
  lookahead check and changes under the prefix test. The page cannot drift: a test compares each
  block with its file, one that every `ta.name(` in the sheet exists, one that every `ta` helper is
  in the sheet, and one that every output kind is in the table.
- **All four passed both checks at the first run**, including the marker-only file (the checks' own
  caveat, that a file with only markers cannot be told apart, did not produce a warning here).
- **The Pine sheet says what has no equivalent** instead of mapping loosely: anything that
  looks ahead (negative `offset`, `x[-1]`), other symbols and timeframes, alerts and strategies,
  `barstate.*`, drawing objects, a colour per bar, and 20 `ta` functions that `ta` lacks (with the
  pandas one-liner where there is one). `ta.pivothigh` is described as Pine reports it (on the
  confirming bar); the `offset=-right` that Pine scripts add to draw it back is the future.
- **Not verified against TradingView:** the sheet states the definitions as the `ta` tests and
  docs/indicators.md already do; the three items marked "to check in TradingView" in the roadmap are
  unchanged.
- `docs/indicators.md`: the `ta` heading no longer says "in progress", and "Not yet" says the usual
  set is available as the four files rather than as built-ins.

## `/api/candles` ends with the forming bar (2026-10-16)

The owner's decision after #26, which had left the candles route alone and asked. The answer now
folds the builder's forming M1 bar into the bar of its period (or adds it as a bar of its own),
for the answer only. Checked first:

- **The cache** (`market/cache.py`) holds complete 4h, D and W bars only and is filled from the
  stored bars (`extend` reads the store, never a frame the reader built). The forming 4h, D or W
  bar is the tail the reader computes from stored M1 and H1 on every read; the live minute is added
  to that frame in memory afterwards. So nothing of it can reach the cache, and the reader copies
  the frame before changing it. Tested against a whole-database snapshot for 1m, 5m, 1h, 4h, 1D and
  1W (with a control that the snapshot sees a stored minute), and by a read with the bar followed by
  one without it.
- **`complete`** was `last_end <= now` for the last bar and true for every other. That would call a
  bar with live data complete when its minute is over but no tick has closed it yet (a quiet
  market: the builder closes it on the next tick or on its clock). A bar with live data is now
  never complete, and carries `forming: true` (absent on every other bar, like `hits` in the
  indicator answer). Only the last bar can have it.
- **Who reads these bars.** `SeriesReader.candles` has two callers: this route and the indicator
  route (already folded in by #26). The alert engine does not use it: it reads stored M1
  (`store.read_m1`) for its catch-up and evaluates on ticks. In the browser: the first load, `resync`
  after a reconnect, `loadOlder` (a `to` before the live minute: no bar), and the watchlist's two
  daily candles. `resync` merges with `mergeBars`, which already ignores an incomplete fresh bar that
  has the time of our last one (a snapshot may be older than the live updates we have), and applies
  one that starts after it; three tests pin that for the forming bar, and the comment that said the
  server's bar lacks the live minute is corrected. The watchlist takes the last two daily bars as
  today and last close and previous close: right after the 17:00 rollover the new day's bar now
  exists from the first live minute, where it used to appear after the first minute had closed.
- **Not applied** (as for the indicators): the window ends before the live minute, a full page,
  sub-minute timeframes (built from ticks, which are buffered live), a builder bar older than the
  last stored bar. `forming=false` on the request gives the stored bars only.
- **The legend's `∅`** came from the indicator route (stored minutes only), fixed by #26. This does
  not change the legend. What it changes: a reload draws the live bar from the first answer instead
  of after the websocket's first message, and other readers of the route (scripts, the watchlist)
  see the same last bar as the chart.
- **A test changed on purpose:** `test_an_indicator_has_a_value_for_the_live_bar_...` in
  `tests/test_forming_bar.py` asserted "`/api/candles` is unchanged". That was the contract this
  replaces; it now asserts the stored-only answer with `forming=false`. The test of the reader's
  default was renamed, as its name spoke of the route.
- With a connection that joined mid-minute (after a start or a reconnect) the first minute is
  partial, in the answer as on the chart.

## CI-3 backend: indicators per chart on the server (D1), 2026-10-16

Design: `docs/custom-indicators-design.md` 1.2, 4 and 5.4. Code: `store/chart_indicators.py`, `api/chart_indicators.py` (the checks, shared), `api/routes_charts.py`, `api/routes_custom_indicators.py`, `api/routes_layouts.py`. No browser change: the M7 page keeps its own list until the frontend step, after the other session's chart work merges. Tests first (`tests/test_chart_indicators.py` with the fake worker, and the migration test in `tests/test_migrations.py`): run red against main with only the tests added, **40 failed and 7 passed**: the unknown-chart test and the six odd delete names, which got a 404 because no route existed. The mutation check below shows that each of them bites.

- **Migration 0008** (`0008_chart_indicators.sql`, the next free number; no open PR has a migration): `chart_indicators` as designed, plus `CHECK (kind IN ('builtin', 'custom'))` and `UNIQUE (source, instrument, position)`, an index on `(kind, indicator)` for where-used, and `chart_indicator_revisions (source, instrument, revision, updated_at)`. Tested only on synthetic databases: one built to 0007 with a layout and a drawing, then upgraded (both untouched, the constraints refuse what they should). **The owner's live database was not touched**; it gets the migration when the owner pulls and restarts.
- **Revisions.** Every write moves the chart's revision on by one; a chart never set is revision 0. The design's "a chart that has no stored instances" became "**a chart whose list was never set**": the import of a browser's old list is refused (409) once a chart was set, also to an empty list. Otherwise a second browser with an old `localStorage` list would bring back indicators the owner had removed. A `PUT` may name the revision it was based on and is refused with the current list (409) if the chart moved on; without one it replaces the list as before (the frontend can start sending it).
- **"Settings reset" (decided here; to confirm).** A write stores the list as it now fits (every input; unknown names dropped; for import and restore, refused values replaced by the default), and that write's answer and broadcast list the reset inputs: the note, once. A read fits the stored values to the definition as it is now **without writing**, so after the file was edited the note shows on every read until the chart is next written, and editing the file back brings the old values back. "Once" on reads is left to the browser (show the note, then write the list back).
- **PUT is strict on values, lenient on names:** an unknown definition, a repeated `uid` or a value its input refuses is a 400; an input the definition does not have is dropped with the note (the brief's rule). Saved layouts now follow the same rule, so a layout entry with an unknown parameter name is no longer a 400 (M7 refused it; no test or browser relied on it). Import and restore never refuse a value: it falls back with the note, and entries whose definition is gone are listed in `skipped`.
- **A file of yours that does not load at the moment** (an error, quarantined, the worker not running): its instances keep their values as given, a browser can write the list back without losing them, and a new instance of it is accepted. Values are checked once the file loads again. Without this, one broken file would block every change on its charts while the owner edits it. A file that is gone is `missing` on reads, a 400 on PUT, skipped on import and restore.
- **Bounds on stored values** (they hold even with no definition to ask): at most 30 instances, 64 values each, a value is `true/false`, a finite number or a text of at most 1000 characters, 8 KB of JSON per instance, `uid` `[A-Za-z0-9_-]{1,40}`, ids `[a-z][a-z0-9_]{0,39}`.
- **NaN in a request body was a 500** (found while writing the tests): Python's JSON reader takes `NaN` and `Infinity`, pydantic refuses them, and FastAPI's 422 then echoes the input, which cannot be encoded as JSON. In CI-3's routes non-finite numbers are refused after parsing, with a plain 422. The same in the existing routes (drawings, alerts) is in `docs/security-review-custom-indicators.md`, not changed here.
- **Kinds and namespaces.** Every instance has `kind`. An entry without one (M7 layouts, the browser's old list, a PUT that leaves it out) is the built-in with that id, or the custom one when only a file of yours has it. Where-used and delete count a layout entry without a kind as custom only when no built-in has the id, and never touch an instance of kind `builtin`.
- **Layouts carry a copy.** A layout saved **without** `indicators` copies its instrument's list from the server; one saved **with** a list (the M7 browser always sends one) keeps that list, checked like a PUT, with `kind` and `uid` added. So the M7 page works unchanged and the new one can leave the list out. **Restoring is `POST /api/layouts/{id}/apply`** (a `GET` of a layout stays a read): it replaces the chart's list with the copy, keeping the uids, onto the layout's instrument or `?instrument=`. Before CI-3 a layout with a custom indicator was refused (only built-in ids were known to layouts): this fixes finding 5 of the review of CI-2 (b) to (d) above, tested by `test_a_layout_saved_without_indicators_copies_the_charts` (an M7 list with a custom entry is saved).
- **The catalog's sections** were already in CI-1 (`builtin`, `custom`, each with `kind`). CI-3 adds `kind` to the `errors` entries (one existing assertion, in `tests/test_worker_windows.py`, gained the field) and pins the sections in a test. The flat `indicators` list still leaves out a file of yours with a built-in's id, until the browser reads the sections.
- **Where-used and delete are behind CI-1's editing gate** (`require_editing`: the minimum isolation, then the setting and the request's origin), as the design gives every new `/api/custom-indicators` endpoint; the chart routes are not (adding an indicator from the phone is not editing code). Delete moves `<id>.py` and `.history/<id>/` to `.deleted/<id>-<UTC time>/` as `<id>.py` and `history/` (`-2`, `-3`, ... if that name exists). Order: check that `.deleted` and `.history` are plain folders inside the indicators folder (409 and no change otherwise: a file, a symlink or junction elsewhere), make the target folder, remove the instances from charts and layouts, then move the files. If a move failed after the instances were removed, the file would stay where it is and the delete can be repeated (now not in use); the other order would leave instances of a file that is gone.
- **One lock for every write to charts' lists** (`ChartIndicators.lock`): PUT, import, apply and delete, so "never set", "the revision I read" and "where it is used" cannot change between the check and the write. The database is one shared aiosqlite connection: the store validates everything before writing and never rolls back, because a rollback would take other writers' uncommitted rows with it (as in the other stores).
- **Broadcasts:** `chart_indicators_changed` with the chart's list (as GET answers it, plus that write's notes), `layouts_changed` when a delete changed layouts, `indicators_changed` (no payload) after a delete.
- **Mutation check** (`tools/mutate_chart_indicators.py`, the runner of `mutate_checks.py`; 52 mutants, one rule broken in each: kinds and namespaces, fitting and the note, strict and lenient paths, every bound, source and instrument, revision, the import's "never set", broadcasts, store order and revision, the migration's constraints, layouts' copy, check and apply, the gate on usage and delete, the id pattern, which layout entries count, confirmation, built-in instances kept, layouts and history moved, the `.deleted` check and the order of the steps, the catalog's `kind`). First run: **49 killed, 3 survived**, each then given a test: another source whose instruments are in the database (the instrument lookup alone hid the source check), `apply` with `?instrument=` on a layout that has an instrument, and a file name ending in a newline (`$` in a pattern matches before it; Linux only, since Windows names cannot hold one, so that mutant is marked equivalent on Windows). Rerun: **51 killed, 1 equivalent** (on this PC).

### CI-3 backend: review of the change (2026-10-16)

An adversarial pass over the pushed diff (PR #33, CI green). Each finding got a test first, shown failing against the pushed commit (12 failures, each on its own assertion: the source tree of that commit with the new tests), then the fix, then a mutant per fix (`tools/mutate_chart_indicators.py`, 9 more: 8 killed, 1 equivalent).

1. **A layout saved during a delete kept the deleted definition** (medium). Layout writes did not take the charts' lock, so a layout saved after the delete had listed the usage, and before it moved the file, still found the file and kept an instance of it. Layout create and update now check and store under the lock (`checked` asserts it). Test: a delete held inside the lock by a slow store, a layout saved meanwhile.
2. **Two deletes at once:** the second looked the file up before the lock, waited, then failed on the missing file (500) and left an empty folder in `.deleted`. The file is looked up again under the lock: one 200, one 404, one folder.
3. **Lax types:** `revision: true` was read as 1 (it would match revision 1), `"0"` as 0, `-1` accepted; `visible: "no"` and `price_labels: 0` were read as booleans. Now `revision` is a whole number of at least 0 and the three switches only `true`/`false` (422 otherwise). The M7 browser sends real booleans.
4. **The import took a list whole or not at all**, with PUT's bounds (30 entries, the value limits). The browser's old list had no limit and was never held to these bounds, so one entry outside them would have kept a browser's list out for good: the import is accepted only once per chart and the browser has nothing else to send. Now the list (at most 200 entries) is read entry by entry, as a layout's copy is in `apply`: what is not an instance, holds a value that cannot be kept (NaN, too large) or comes after the first 30 is listed in `skipped` with the reason, the rest is imported.
5. **A refused body holding a NaN was a 500**, for 31 entries on PUT or 201 on the import: FastAPI's 422 echoes the input, and the JSON response cannot encode NaN. The app's 422 handler now sends such numbers as text (`"nan"`); it is otherwise FastAPI's own response. It applies to every route, so it also fixes the same 500 on drawings and alerts found by the security review (`docs/security-review-custom-indicators.md`, S3), whose test is a plain test now.

Considered and kept as they are: chart writes wait under the lock while a definition is described (a worker that is starting, a file just edited), so a slow describe holds other charts' writes for as long as the values requests already wait (the job deadline, 90 s at the worker's very first start); doing the checks before the lock would let a delete slip in between the check and the write. An old layout without uids gets new ones at every `apply`.

### Security review of the custom-indicator path (2026-10-16)

`docs/security-review-custom-indicators.md`: read-only, at `11e3d6a`. Nine findings, none high: S1 (medium) `auto` editing trusts any loopback peer without a proxy header, so a local forwarder such as an SSH tunnel makes a remote browser count as local (suggested: also require a loopback `Host`); S2, S3, S4, S6 (low): a deeply nested header from the worker ends the reader thread (a timeout instead of a protocol error), non-finite or huge numbers in `params` are a 500, the server reads up to 256 MB of a worker's arrays before checking them, a script can write frames on the protocol stream and answer for the next request; S5, S7, S8, S9 (info, mostly for CI-4). Each open one has a failing test in `tests/test_security_review.py` (`xfail(strict=True)`); none is fixed here, for the owner to decide. Five more were in CI-3's own diff and are fixed there (see the review section above).

## CI-3 frontend, steps 1 to 3: the sections, the states, deleting a definition (2026-10-16)

The plan was approved with three decisions: (a) the default layout at start sets the
non-indicator fields only; (b) a legacy list is imported once per chart at revision 0, never an
empty list, the server's list is adopted on a 409, the legacy key is dropped once every enabled
chart has a list, and a one-time toast says the old list was copied to this chart (M7's list was
shared by all instruments); (c) `API_VERSION` is bumped so older clients warn. (a) to (c) belong to
steps 4 to 6 and wait for #33 to merge. This is steps 1 to 3, on top of what `main` sends today.

- **`instrument.source` is the `{source}` of the chart routes.** `/api/instruments` lists only the
  server's own source (`store.instruments(server.source)`), `Instrument.source` is in the client's
  type, and #33's `known_chart` compares the URL's source with `server.source`. Both path segments
  are encoded one by one (`chartPath`). Checked against #33's real route in the browser pass.
- **`API_VERSION` is not bumped here.** Version 2 means #33's routes exist, so it goes with step 4;
  bumped now it would announce routes `main` does not have.
- **Errors keep their body.** `request()` read `detail` as a string or a validation list, so #33's
  409 (`detail: {message, usage}`) would have become "Conflict". `ApiError.detail` keeps it whole
  (`apiErrorFrom`).
- **The picker is a model plus a drawing** (`ui/pickerModel.ts`, `ui/indicatorDialogs.ts`): Built-in and
  Your files, one search over title, name, id and file, and every file of yours listed with its
  state instead of left out: `error` (reason, line), `quarantined` (edit the file to retry),
  `not_run` (the worker's own error), and `shadowed`, a file with a built-in's id. That last state is
  new: instances are keyed by id alone until step 4 gives them a kind, so such a file cannot be added yet.
  The isolation notice and the worker's own-folder note sit on Your files; a built-in's load error
  is a notice on Built-in. A server from before the sections still gets both sections, from the flat
  list and `errors`.
- **Deleting** asks `usage` first and lists charts and layouts when the file is in use; the answers are
  `definitionDelete.ts`. 403 shows the server's reason; a 404 that says only "Not Found" means the
  server has no such route (this server, until #33 merges); a 409 on the plain delete (it became used
  meanwhile) asks again with the list; a 5xx or no answer re-reads everything, since #33's delete
  removes the instances before it moves the file and a failure in between leaves the two out of step
  (review finding of #33). Whether the editing gate is open decides the trash button; when it is not,
  the button stays visible with the server's reason as its tooltip (`aria-disabled`, not `disabled`:
  a disabled button shows no tooltip).
- **Instances in this browser's list.** Until the list moves to the server (step 4), the legacy
  entries of a deleted file are dropped from the chart once a re-read catalog no longer has it
  (`IndicatorLayer.removeDefinition`), so nothing is left on the chart as "Could not compute".
- **Checked.** 47 new unit tests (the API helpers, the picker model, the delete flow); a mutation check
  of those: 41 mutants, 41 killed (one gap in the de-duplication test closed, one equivalent special case
  deleted). `IndicatorLayer.removeDefinition` and the dialogs have no DOM test (the frontend tests run
  in node): a Playwright pass on a scratch server with the catalog and the delete answers intercepted
  (every state, 403, 404, 409, 500, both themes), and a real run of my build on #33's backend (charts'
  lists and a layout made through #33's routes, the dialog lists them, the delete removes them, the file
  is in `.deleted`, the broadcasts arrive): 18 of 18.

## CI-3 frontend, steps 4 to 6: the list on the server, the import, layouts (2026-10-17)

The owner's decisions for these steps: (a) the default layout at start takes the non-indicator fields only;
(b) the old list is imported once per chart at revision 0, never empty, the server's list is adopted on a 409,
the old key goes once every enabled chart has a list, and a toast says the old list was copied to this chart
(M7's list was shared by every instrument); (c) `API_VERSION` is bumped so older pages warn.

- **One list per chart, read from the server.** `ChartSync` (`chart/chartSync.ts`, no DOM, tested with a
  fake server that has the 409 rules) reads the list when a chart is opened (`GET`), writes every change as
  the whole list with the revision it was based on (`PUT`), and follows `chart_indicators_changed`. The
  chart's own `instrument.source` is the `{source}` of the route (checked against the server: the same
  value `GET /api/instruments` returns and #33 compares).
- **Server wins.** A 409 on a save shows the server's list and says the last change was not saved; a list
  another browser sent while ours was waiting drops our waiting edit too (it was based on the older one).
  The alternative, saving again over the newer list, would silently undo someone else's change.
- **One save at a time, the newest list per chart.** Changes made during a save go out as one save after
  it. A save is always sent to the chart it was made on, also when the user has moved to another chart
  since. Opening a chart waits for the saves still on their way (otherwise the read could show the list
  from before the user's own last change).
- **Our own write is not shown twice.** The server broadcasts before it answers, so a message for a chart
  with a call under way waits for the answer, then is compared with the revision the answer gave.
- **Nothing is written to a chart whose list is not on screen.** While a new chart's list is being read the
  legend is empty and changes are not saved; when the read failed they are refused with a toast, because
  saving a half list over the real one would lose indicators.
- **A placeholder keeps what the server cannot draw.** An instance whose definition is gone, whose file has
  an error, is switched off (quarantined), cannot run (worker off) or shares an id with a built-in stays in
  the list as a legend row with the reason and Remove, and is saved back as it was (its kind and values
  untouched). Before, `setEntries` dropped unknown ids, which with the list on the server would have
  deleted them for good on the next save. One exception, found in a browser run: the server refuses a
  save that holds an instance with no definition at all (`400`, "no custom indicator ..."), so those
  (status `missing` from the server, not just absent from a catalog that may have failed to load) are left
  out of the next save, the list is read back, and a toast names them. Without that every later change
  would have failed until the user removed the row by hand. `shadowed` (same id as a built-in, listed as `custom`) is new on
  the client: the values request has no `kind` yet, so that instance cannot be drawn.
- **Entries carry `uid` and `kind`.** The browser makes the uid of a new instance (letters and digits,
  well inside the server's rule) and sets `kind` from the definition, so a save never leaves the server to
  guess. Only the layer assigns them; the settings dialog keeps the ones the entry had.
- **The old list.** `prefs.indicators` is read apart (`readLegacyIndicators`) and no longer part of the
  preferences the page works with, but `savePrefs` keeps it in storage until `dropLegacyIndicators`, so a
  theme change does not lose it. It is offered to a chart at revision 0 only, once per chart and page, and
  never empty. After the first open, and after every import, the other enabled charts are read; when all
  have a list the key goes. A chart that is never opened keeps the key (it is a few hundred bytes, and the
  chart gets the list the day it is opened). A 409 on the import is not an error: another browser was first.
- **Layouts.** Saving a layout sends no `indicators`: the server copies the chart's own list, after the
  page's pending saves have been answered. Loading one calls `POST /api/layouts/{id}/apply` for the chart on
  screen (always with its instrument given) and shows the chart the server answers with; entries it
  could not use are named in a toast. The default layout at start only sets the other fields, as decided.
- **The "settings reset" note.** The legend row shows a "Settings reset" mark and a toast names the inputs,
  once per revision. The mark goes with the next save, as the server's note does.
- **After a delete here** the instances are removed from the legend without saving (the server already
  removed them and broadcasts the lists); other browsers also re-read the catalog on `indicators_changed`.
- **API version 2** (server `API_VERSION`, client `CLIENT_API_VERSION`). A page older than the server
  warns as before; a page newer than its server now also warns, since it cannot save indicators there.
  A test reads the server's number from `src/openchart/__init__.py` so the two cannot drift.
- **Checked.** 83 new unit tests (the model, the controller against a fake server, the preferences, the
  version notice); a mutation check of the new modules: 59 mutants, all killed (14 survived a first run
  because the tests were too loose; they were tightened, and one equivalent change removed). A Playwright
  pass on a scratch server with the fake broker (31 checks: the import with its toast and the key going,
  lists per chart, a second browser following, a file with an error kept and saved back, an instance
  whose file is gone left out with a note, the reset note and its removal, a layout saved and loaded, an
  entry left out with a note, the default layout leaving the list alone, the older-server banner), dark
  and light.
- **Not done.** The values request still has no `kind`, so a custom file whose id is also a built-in's
  cannot be drawn; the picker keeps saying so. A toast appears for every change another browser makes
  (4 seconds); it is cheap to remove if it is too much.

## Hardening after the security review (2026-10-17)

The owner approved the plan in `docs/security-review-custom-indicators.md` ("After the review"): S1, S2, S3, S5, Session A's findings 1 to 3 of CI-2 (b)-(d), the non-atomic delete and the empty import are fixed; S4, S6, S7, S8, S9 and the change stamp are documented. Tests first: 27 failed against main before the code (each on its own assertion); three passed already and the mutation check shows they bite (the database-failure delete, which today's order happened to survive, and two guards: no rerun for a few more bars, times in range still accepted).

- **S1, owner's choice:** `auto` is off, from everywhere; `on` from the CLI. A variant (keep `auto` and add a loopback `Host` check) was weighed and dropped: an SSH tunnel keeps `Host: localhost`, so it would not have closed the case that started this.
- **S2, owner's rule:** "counts only if it repeats" became "counts only when no other script had run in that worker". After a bad frame the worker is replaced, so a script that breaks the stream itself is alone on the next worker and is quarantined after three; a victim of another script's late bad frame never is. The script that breaks the stream after its own answer is not blamed either (it answered well): each time it costs a worker restart, and it shows as `protocol` failures of others in the log.
- **The skipped check:** a check that had too few bars for one of its runs is checked again by a request with at least twice its bars (so at most a handful of reruns per settings), in place of the short one; the five-settings cap is unchanged.
- **from/to/limit:** bounded at the API (2**53 for times: what a browser holds exactly; 2**31 for `limit`), 422 like any other refused query.
- **The delete, all or nothing:** files move first (`<id>.py`, then `.history/<id>`), then the instances are removed; a failed move is a `409` with nothing changed, a failed database step puts back the charts and layouts already changed (best effort, they were written one by one) and moves the files back, then fails.
- **The empty import:** no write and `imported: false` (revision stays 0); `imported: true` otherwise.
- **The Settings dialog** showing the editing state and reason read-only is left to the frontend session (Session A): the catalog's `editing` already carries `{allowed, setting, reason}` with the command to run.
- **Mutation check** (`tools/mutate_hardening.py`, 27 mutants: the gate's three rules, the deep header caught, the reader's bad-frame end, the quarantine rule in four ways, the worker kept after a bad frame, the whole-number and overflow checks, each of the six bounds, both id patterns, the rerun rule in four ways, the delete's three undo paths, the empty import): **25 killed, 2 equivalent**: the built-in registry's pattern (only files shipped in the package reach it, and no name there can hold a newline on Windows) and a rerun while the short check is still pending (a request answered meanwhile has its own result). Some kills are reported by the runner under a log line that starts with `ERROR` instead of the test's name; the verdict is the exit code, so they are kills.
- **Found later, in the full run of the stacked CI-5 branch (test first, red on Windows every time):** on Windows a file another thread or process has open cannot be renamed (WinError 32, Python opens files without `FILE_SHARE_DELETE`), so a catalog read of `<id>.py` at the moment of a delete made the delete a spurious 409 "could not move the file". The delete's moves now go through `files.move`, which retries for up to a second while Windows reports the file in use (and fails at once for anything else, and on Linux and macOS), in a thread so the event loop does not wait. CI-4's saves use the same helper.


## CI-4: the API contract, before the code (2026-10-17)

`docs/api.md`, "Your indicator definitions": the write endpoints of design section 4, written first so the editor (Session A) can be built against them. Choices made here, for review:

- **Templates:** `blank` plus the four tested examples of docs/custom-indicators.md, shipped in the package; New takes a template id and an optional title.
- **Import never overwrites:** an existing id is a `409` with a `suggested` free id; without an id it is made from the file name.
- **Save never creates** (`404`; New and Import create), keeps the last 10 versions in `.history/<id>/`, and takes an optional `base_version` so two tabs (or the editor and a text editor) cannot overwrite each other silently: `409` with the stored version.
- **Restore is a save** (undoable); Rename edits the single `TITLE = "..."` line or answers `409` "edit TITLE in the code" (design 1.1).
- **Preview stores and counts nothing:** no file, no history, no quarantine count, no save-time checks; same query and bars as the values route.
- **Sources:** 256 KB as UTF-8, NUL and lone surrogates refused, a leading BOM dropped, line endings kept; writes atomic (temporary file and rename) and serialized per id; plain files only, never through a link.
- **`indicators_changed`** gains `{id, op}` (compatible: pages that ignore the fields still refetch).

## CI-4 backend: the editor's endpoints (2026-10-17)

The contract of #37, implemented (`api/routes_definitions.py`, `indicators/_files.py`, `indicators/templates/`). Built on the hardening branch (#40), not on main: request ids need its `protocol` failure kind, and the two would otherwise have duplicated and conflicted in `runner.py`; the PR says "depends on #40" and also carries #37's docs. Tests first (`tests/test_definitions_api.py`, `tests/test_request_ids.py`): 102 failed before the code.

- **Hostile-input pass**, each a test: ids with `..`, `/`, `\`, capitals, a trailing newline or space, a leading digit or `_`, `.py`, 41 characters, empty, and **Windows device names** (`con`, `nul`, `com1` ...: refused everywhere, since on Windows `con.py` is the console; found by this pass, not in the contract); versions with `..`; 256 KB exactly (taken) and one byte more, also 256 K characters that are more bytes as UTF-8 (413); NUL, a lone surrogate (400); a BOM (dropped) and CRLF (kept); a folder named `x.py`, a symbolic link (Linux CI; this PC needs a privilege), **a hard link** (no privilege needed on NTFS: refused, found by this pass), a `.history` junction and a `.history` file (409, nothing written); a hand-written Latin-1 file (409, not rewritten); a failed rename (the old file stays, no temporary file, no history version); 8 concurrent saves (whole files, 8 distinct versions), 4 concurrent saves from one base (one through), 4 concurrent News of one id (one created); nothing written outside the folder.
- **New never overwrites,** also against a writer outside the server: the file is published with a rename that fails when the name exists (`os.rename` on Windows, `os.link` then unlink on POSIX), not `os.replace`.
- **A failed save leaves no version:** the current file is kept in the history first; if the rename then fails, that version is removed again.
- **Preview** goes to the runner directly (describe, warmup, compute, the server's own check of the answer), never through `CustomIndicators`: nothing is stored or counted. It shares the values route's bar reading and answer shaping (`read_bars`, `shape`), so its bars are the chart's.
- **After every write the catalog reads the file again** (`CustomIndicators.forget`), whatever its time and size say: this closes Session A's finding 4 (same-size edits) for saves made in the editor.
- **Request ids (S6):** see the security review's CI-4 section. Answers carry `rid`; probes and arming too.
- **The delete's broadcast** carries `{id, op: "delete"}` like the others.
- **Not done here:** an `API_VERSION` bump (the frontend session decides when its editor needs these routes), the Settings display of the gate (frontend).
- **Review of this change (finding 1, test first):** a delete did not wait for a save of the same file, so a save paused between keeping its history version and writing the file wrote it again after the delete had moved it (the test held the save there and showed the file back). Every write of `<id>.py`, the delete included, now holds the file's own lock (`CustomIndicators.file_lock`); the delete takes the charts' lock first, the editor's endpoints only the file lock, so no two locks are ever taken in the other order.
- **Mutation check** (`tools/mutate_definitions.py`, 36 mutants: ids and device names, the import's id, BOM, NUL, size in bytes, links, junctions, hard links, plain files, the history folder, New never overwriting, `base_version`, the history kept, ten versions, the atomic write, a failed save's version, version order, the TITLE rules, `(copy)`, the gate on four kinds of routes, serialized saves, Preview's check of the answer, request ids three ways): **34 killed, 2 equivalent** (the history-inside check, which a link check before it already covers; and reading the file again after a write, which NTFS's changed time already forces). Two mutants needed repair first: "BOM kept" left an indentation error (it was "killed by an error", which proves nothing) and "New overwrites" no longer matched the reformatted code; both then killed by their tests.
