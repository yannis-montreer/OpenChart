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
- **No candle cache table yet.** Daily and weekly requests resample all stored M1 (about 1.4 s for 2 years of gold); add the optional cache if the chart needs it.
- **The derived-vs-OANDA candle check is a live test** (`pytest -m live`), so it runs with the owner's credentials and not in CI. No market-data fixtures are committed (SPEC section 14).
