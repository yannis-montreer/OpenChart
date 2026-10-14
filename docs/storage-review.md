# Storage review (2026-10-14)

Analysis only. Everything here was measured on a copy of the owner's `data\` folder
(SQLite online backup, read-only on the source) with XAU_USD and EUR_USD, each with
2 years of M1 and deep H1. Nothing in the live folder was changed. Proposals are
marked for approval; none is implemented.

## 1. What takes the space

### Files in `data\`

| Item | Size | Notes |
|---|---:|---|
| `openchart.db` | 275.6 MB | 30 MB of it is slack: `VACUUM` brings it to 246.2 MB (7.9 s) |
| `openchart.db-wal` | 4.6 MB | bounded: SQLite checkpoints at about 4 MB |
| `openchart.db-shm` | 32 kB | |
| `ticks\` | 1.4 MB | tick storage only just started (one full day of gold) |
| `backups\` | 4 kB | `openchart backup` (M8) writes here by default and never deletes |
| `indicators\` | 0 | |
| `logs\` | none | a source checkout logs to the console; the service writes `logs\service.log` |

### Tables (each rebuilt alone and measured, so the slack is excluded)

| Table | Rows | Size | Bytes/row |
|---|---:|---:|---:|
| `candles_m1` | 1,449,799 | 198.2 MB | 136.7 |
| `candles_h1` | 264,233 | 36.1 MB | 136.5 |
| `candles_cache` (4h/D/W) | 83,787 | 11.8 MB | 141.1 |
| everything else (drawings, alerts, settings, closures...) | < 100 | < 0.1 MB | |

Per instrument (rows, and size at today's 137 B/row):

| | M1 (2 years) | H1 (deep) | Cache |
|---|---|---|---|
| XAU_USD | 707,838 rows, 96.8 MB (from 2024-10-13) | 125,190 rows, 17.1 MB (from 2006-03-19) | 39,397 bars, 5.6 MB |
| EUR_USD | 741,961 rows, 101.4 MB (from 2024-10-14) | 139,043 rows, 19.0 MB (from 2002-05-06) | 44,390 bars, 6.3 MB |

**M1 is 80% of the database, and it is not a fixed window.** The download goes back
2 years once; from then on the live tail adds about 360,000 rows (about 50 MB) per
instrument per year and nothing is ever deleted. After 5 years an instrument holds
about 250 MB of M1. H1 grows by about 6,000 rows (0.8 MB) a year.

Why 137 bytes a row: every row repeats the key as text (`"oanda"`, `"XAU_USD"`, about
15 bytes with their headers), and the 12 prices are 8-byte floats (SQLite stores a
fractional REAL in 8 bytes whatever its value), plus record headers.

### Ticks

| Instrument | Measured | Bytes/tick | Per full trading day | Per year (260 days) |
|---|---|---:|---:|---:|
| XAU_USD | 13 Oct: 81,518 ticks, 787 kB (compacted, zstd) | 9.7 | 0.79 MB | about 205 MB |
| EUR_USD | 20 minutes in the afternoon: 6,241 ticks/h vs gold's 4,758/h at the same time | (similar) | about 1.0 MB (estimate) | about 270 MB |

The EUR_USD figure is an estimate from 20 minutes; a full day will tell. Today ticks
are small, but kept forever they outgrow M1 within a year: about 475 MB a year for the
two instruments, against about 100 MB of new M1.

Files of the current day are one part file per minute (about 3 kB each, 20 B/tick)
until the day is compacted into one `data.parquet` after midnight UTC.

## 2. Shrinking `candles_m1` without losing the exact match with OANDA

Facts measured first, on all 1.45 million rows:
- Every price is exact at a fixed number of decimals: gold 3, EUR/USD bid/ask 5, and
  EUR/USD M1 mid 6 (OANDA's mid has half-pips). So `round(price * 10^(decimals+1))`
  as an integer, divided back, gives the same float bit for bit (checked on every
  value of every column).
- **Mid open and close** are `(bid + ask) / 2` rounded by OANDA: 75% exactly, the rest
  one unit off in the last digit. They can be stored as a tiny correction (-1, 0, +1),
  not dropped.
- **Mid high and low cannot be derived**: in about half the rows they differ from
  `(bid_h + ask_h) / 2` by up to tens of pips (the mid's high is not at the same tick
  as the bid's and ask's highs). They must stay stored.

Measured sizes of `candles_m1` (all rows), each layout checked to give back every
price exactly:

| Layout | Size | Bytes/row | Saving |
|---|---:|---:|---:|
| Today: 12 REAL, text key | 198.2 MB | 136.7 | |
| Today with page size 8 kB / 16 kB / 64 kB | 196.4 / 195.5 / 195.0 MB | | 0.9 / 1.4 / 1.6% |
| A: 12 scaled INTEGER, text key | 116.8 MB | 80.5 | 41% |
| A+C: scaled INTEGER, small integer instrument key | 97.8 MB | 67.5 | 51% |
| B: deltas, text key | 83.5 MB | 57.6 | 58% |
| **B+C: deltas, integer key** | **65.1 MB** | **44.9** | **67%** |
| (reference) the same rows as zstd Parquet | 45.2 MB | 31.1 | 77% |

- **A, scaled integers:** prices as `INTEGER` times `10^(decimals+1)`, the scale stored
  per instrument. SQLite stores these in 3 or 4 bytes instead of 8.
- **B, deltas:** `bid_o` as a scaled integer; `bid_h`, `bid_l`, `bid_c` as distances
  from `bid_o`; the four ask prices as the spread over the bid; `mid_h`, `mid_l` as
  distances from `bid_h`, `bid_l`; `mid_o`, `mid_c` as the rounding correction (0 most
  of the time, which SQLite stores in no bytes at all). Small numbers take 1 or 2 bytes.
- **C, integer key:** `(instrument_id, ts)` instead of `(source, instrument, ts)`, the
  ids in the `instruments` table. Saves about 13 bytes a row.
- **Page size:** not worth it (under 2%).
- **VACUUM:** 276 to 246 MB once (11%), see below for `purge`.

The same layouts apply to `candles_h1` (36 MB, about 12 MB with B+C) and
`candles_cache` (12 MB, about 4 MB). For the whole database of the two instruments:
about 246 MB today, about **85 MB with B+C** on all three tables.

What each costs:

| Option | Code | Risk |
|---|---|---|
| VACUUM in `purge` | a few lines | none; needs the server stopped (purge already requires it) and free disk equal to the database |
| A | conversion in `CandleStore` (writes and every read of price columns), a migration that rewrites the tables, the scale in `instruments` | low: one place converts, exactness proven by a test over all stored rows |
| A+C | as A, plus the key change in every candle query | low to medium: more SQL touched |
| B+C | as A+C, plus vectorised encode/decode (about 30 lines of numpy) | medium: the encoding must be exactly reversible; a round-trip test over all stored rows and the existing match-with-OANDA tests cover it. Prices can no longer be read with plain SQL (no feature does that today) |

The migration framework runs SQL files only; rewriting tables with encoded values
needs a Python migration step (or SQL arithmetic: A is plain SQL, B is too, with
`CAST(ROUND(...))`). On the owner's database the rewrite would take well under a
minute.

**Recommendation:** B+C for all three candle tables, in one migration, after the
current branches are merged. If you prefer the smallest change, A+C gets half the
saving with the simplest code.

### Should `purge` run VACUUM?

Yes. Deleting EUR_USD's rows from the copy freed 30,923 pages but the file stayed at
246.2 MB; SQLite reuses free pages but never gives them back to the disk. VACUUM then
brought it to 119.5 MB in 4.3 s. `purge` already refuses while a server runs, which is
what VACUUM needs. Proposal: VACUUM by default, print the size before and after, and
`--no-vacuum` to skip it.

## 3. Tick retention (proposal)

Ticks are used for second charts (1s to 30s) of past periods and for future replays;
the API serves at most the last 3 days of them. Proposal:

- Setting **"Keep raw ticks for"**: 7 days, **30 days (default)**, 90 days, 1 year,
  forever; and "don't store ticks" (today's switch). Default 30 days: about 21 trading
  days, about 17 MB for gold and 21 MB for EUR/USD at today's format, enough to look
  at second charts of the last few weeks; "forever" costs about 200 to 270 MB per
  instrument per year.
- **Expired files are deleted** at the daily compaction (just after midnight UTC): any
  `date=` folder older than the setting. One log line says how much was freed.
- **Compression:** already zstd Parquet. Measured on gold's full day: storing prices
  as scaled integers with the ask as a spread and delta encoding brings 787 kB to
  405 kB (49% smaller, lossless, microsecond timestamps kept). The reader would accept
  both formats, so no conversion of old files is needed. zstd level 19 alone gives 19%.
- Code: small (a setting, a deletion pass in `compact_closed_days`, the encoding in
  the compaction step). Risk low.

## 4. M1 depth per instrument (proposal)

Today every instrument gets 2 years of M1 (100 MB at today's format, about 34 MB with
B+C) and keeps growing. Proposal:

- When adding an instrument (and later in Settings, Instruments), a choice:
  **3 months, 1 year (default for new instruments), 2 years, 5 years**. Gold keeps its
  2 years.
- It becomes a **rolling window**: once a day, minutes older than the depth are
  deleted (the cache bars there are then built from H1, which matches to within
  OANDA's own rounding). Making it longer downloads the missing minutes, newest
  first; making it shorter frees the space (with VACUUM, or reused by new rows).
- Sizes per instrument at today's format / with B+C: 3 months 12 / 4 MB, 1 year 50 /
  17 MB, 2 years 100 / 34 MB, 5 years 250 / 85 MB.
- Wording in the add dialog:

  > **Minute history: 3 months · 1 year · 2 years · 5 years**
  > How far back charts from 1 minute to 30 minutes can be scrolled. Charts of 1 hour
  > and longer always go back as far as OANDA has history (for EUR/USD to 2002),
  > whatever you choose here. One year takes about 17 MB. You can change it later in
  > Settings.

  And in Settings, next to each instrument: "Minute history: 1 year (back to
  14 Oct 2025)" with the same choice.
- Code: medium (a column in `instruments`, the choice in the add dialog and Settings,
  the daily pruning, the download target per instrument). Risk low: the newest-first
  download already handles a target.

## 5. Logs

- **The Windows service (M8 branch):** NSSM writes `logs\service.log` and rotates it at
  10 MB (`AppRotateFiles 1`, `AppRotateOnline 1`, `AppRotateBytes 10485760`). NSSM 2.24
  renames the full file with a timestamp and **never deletes old ones**, so the folder
  grows without limit.
- **Docker (M8 branch):** `docker-compose.yml` sets no logging options, so Docker's
  default `json-file` driver keeps all output without limit.
- **What fills them:** uvicorn's access log is on at the default INFO level, one line
  per HTTP request. Measured in a browser on the fake server: an open chart makes about
  2 requests a minute, about **52 a minute with one indicator** (the indicator's last
  values are re-fetched once a second while a bar forms). That is about 75,000 lines,
  roughly 15 MB, a day per open browser, so a rotation every day or so. Without a
  browser the server writes a status line a minute (about 0.2 MB a day).
- Proposals: (a) access log off by default (requests that fail are still logged as
  warnings by the app), `OPENCHART_ACCESS_LOG=1` to turn it on; (b) the server deletes
  rotated `service-*.log` files beyond the newest 5 when it starts and once a day (NSSM
  cannot); (c) compose gets `logging: {driver: json-file, options: {max-size: 10m,
  max-file: "3"}}`. All small, no risk. They touch the M8 branch, so after it merges.

## 6. Build outputs

`dist\` holds 527 MB: the zips (86 MB) and checksums, the unzipped release folders
(190 MB) and PyInstaller's work folders in `dist\.build` (255 MB). `build\` and `dist\`
are both git-ignored (`.gitignore`, lines 23 and 24). `packaging\clean-build.ps1`
removes everything in them except the final zips and `SHA256SUMS.txt`; `-WhatIf`
only lists. `.venv` (285 MB) is the development environment and is not part of any
installation.

## 7. Per instance

See [hosting-notes.md](hosting-notes.md).

## Decisions (owner, 2026-10-14)

To implement after M8 and M9 are merged, not before:
1. **Candle layout A+C**: scaled integers (`10^(decimals+1)`) and a small integer
   instrument key, in one migration, with a round-trip test over every stored row and
   a check that derived bars still equal OANDA's candles. B+C (deltas) is deferred.
2. **Tick retention** default 30 days; choices 7 days, 30, 90, 1 year, forever, off.
3. **M1 depth** for new instruments: default 2 years, with 3 months and 1 year as
   choices, and the wording of section 4.
4. **`purge` runs VACUUM** by default; `--no-vacuum` skips it.
5. **All three log changes** of section 5, after M8 merges.
6. **5s candles**: the spread/offsets layout, default retention 1 year (gold's about
   58 MB a year is acceptable). Plain integers only if a smaller footprint is asked for.
7. **Dukascopy**: nothing downloaded or measured now. It is an optional opt-in provider
   for personal installs only (its terms forbid commercial use and building a
   database), never bundled, never offered on hosted instances, and to be revisited
   with the multi-source work.

**Order of implementation**, once M8 and M9 are merged: the A+C candle migration;
tick and M1 retention with purge and VACUUM; the log fixes; then 5s candles and replay
as separate milestones.

# Part 2: replay, 5-second candles, historical ticks, cold storage

Analysis only, nothing implemented.

## 8. Replay from stored candles

### Which data drives a replay

A replay has a cursor time T that moves forward in steps. What the chart shows is
everything up to T and nothing after it. The finest stored data at T decides how
fine the steps can be:

| Period | Data | Steps | Timeframes that can be replayed |
|---|---|---|---|
| inside the tick retention (30 days) | ticks | any, down to one tick | all, 1s and up |
| inside the 5s retention (section 9) | S5 candles | 5 s | 5s and up |
| inside the M1 depth (2 years) | M1 | 1 min | 1m and up |
| back to 2006 (gold) / 2002 (EUR/USD) | H1 | 1 h | 1h, 4h, D, W |

The page offers the steps the period allows and says why a finer one is not there
("Minute data starts on 14 Oct 2024; before that, replay moves an hour at a time").

### Playing inside a bar

Viewing 4h while stepping by minutes means the last 4h bar grows minute by minute,
exactly as it did live. Proposal:
- The server keeps the single definition of bar alignment (`market/sessions.py`). A
  replay endpoint returns the fine rows for a period (ticks, S5, M1 or H1) with each
  row already tagged with the start of the chart bar it belongs to (the vectorised
  `bar_starts`), in pages of a few thousand rows.
- The browser holds the chart's complete bars before the replay start (one normal
  `/api/candles` request with `to=start`), then for each step merges the next fine
  rows into the forming bar of their tag (open from the first, high and low widened,
  close from the last, volumes added). When the tag changes, the forming bar is
  complete and a new one starts. No alignment code is needed in the browser, and
  stepping needs no request per step: rows are fetched ahead in pages.
- Indicators: the values of complete bars come from the server for bars up to T (the
  existing endpoint with `to`); the forming bar's value is fetched when a bar closes
  and at most once a second while playing, with the forming bar's OHLC sent along, so
  the server computes it on the replayed bar, not on the stored one. No value ever
  uses data after T.
- Speed: play, pause, step forward one step, and speeds such as 1, 5, 20, 100 steps a
  second. Stepping backward reloads from the start of the bar.

### Keeping replay away from live drawings and alerts

- Replay is a mode of one chart in one browser. The server's feed, alert engine,
  stored candles and other browsers are not affected; nothing is written to the
  database.
- While a chart replays, live updates for it (ticks, candles) are not applied, and
  the chart shows a clear "Replay" banner with the cursor date.
- **Live drawings** are shown read-only (they belong to the instrument, and seeing
  them during a replay is useful) but cannot be moved or deleted.
- **Practice drawings** made during a replay live in a separate in-memory layer of
  that browser, are discarded when the replay ends (or saved as a named "replay
  session" later), and are never sent to the server or the alert engine.
- **Alerts** are not created during a replay. The page can show where alerts *would*
  have fired, computed in the browser with the same line geometry (`linePrice`,
  checked against the server by the shared fixture), as markers on the chart. Nothing
  reaches Telegram, the alert log or the engine.

### What the code needs

A replay endpoint (rows with bar tags), a replay mode in the chart view (frozen live
updates, a forming-bar builder from tagged rows, the cursor), the practice drawings
layer, indicator values for a supplied forming bar, and the controls. Medium size,
low risk: it reads only, and live code paths are untouched when no replay runs.

## 9. A 5-second timeframe

### What OANDA has (probed 2026-10-14, REST only)

OANDA has S5, S10, S15 and S30 candles but nothing below 5 seconds. Probed with
`from=2000-01-01, count=3` (the first candle the account can get) and one page of
5,000 candles at a few dates:

| | First S5 candle | Density then | 2012 | 2020 | 2026-10-13 (a full Tuesday) |
|---|---|---|---|---|---|
| XAU_USD | 2006-03-18 20:49:55 UTC | 113 per hour | 568/h | 663/h | 16,239 of 17,280 slots |
| EUR_USD | 2002-05-06 20:59:55 UTC | very sparse (5,000 candles span 2.6 years) | 388/h | 662/h | 13,453 of 17,280 slots |

So S5 history reaches as far back as H1, but it only becomes dense around 2010. OANDA
leaves out five-second periods without a price change (only 78 to 94% of the slots of
a busy day have a candle).

The probe used 42 REST requests in all (a second script re-ran the first one by
mistake), one second apart, and no price stream.

### Size

Measured on the full day of 2026-10-13 (bit-exact integers, `10^(decimals+1)`):

| Parquet layout | XAU_USD per day / per year | EUR_USD per day / per year |
|---|---|---|
| integers, zstd | 566 kB / 147 MB | 245 kB / 64 MB |
| integers, Parquet delta encoding + zstd | 374 kB / 97 MB | 133 kB / 34 MB |
| ask as spread, mid as offsets from bid, zstd | 223 kB / 58 MB | 116 kB / 30 MB |
| same, delta encoding | 281 kB / 73 MB | 105 kB / 27 MB |

A year is 260 trading days; gold's day was a busy one, so a year may come out lower.
Plain integers with Parquet's own delta encoding need no code of ours to encode, but
gold then takes about 97 MB a year. The relative layout (the "B" idea, applied to a
new file format with no migration) brings gold to about 58 MB and EUR/USD to 30 MB.

### Storage and the boundary with recorded data

- Files: `data/candles_s5/<source>/<instrument>/<YYYY>/<YYYY-MM-DD>.parquet`, prices as
  integers, never in SQLite.
- **Today's file** is built from recorded ticks as they arrive (the candle builder
  already makes 5s bars live), written once a minute like the tick part files.
- **OANDA's own S5 replaces it**: once a minute, the last 15 minutes of S5 are fetched
  and written over the tick-built ones (one request), exactly like the M1 refresh. So
  everything older than about a minute is OANDA's candle, and the boundary between
  recorded and fetched data is always the last minute. A 5s chart or a replay reads
  the files in order and does not need to know where the boundary is; the forming bar
  is the live one.
- **History**: the 5s retention period is downloaded newest first in the background,
  like M1 (about 4 requests per day of history, 1,000 for a year, a few minutes).
- **15s and 30s** charts are built from S5 (exact, 15 and 30 are multiples of 5).
- **1s** charts only exist where ticks are kept (30 days by default). A second without
  a tick has **no candle**, like OANDA's S5: no flat candle is invented. The chart
  shows the next second that had a tick, and a replay stepping by seconds simply shows
  no change for a quiet second.

### Retention

Same choices as ticks: 7 days, 30, 90, 1 year, forever, off. The rule "default 1 year
if it stays under about 50 MB per instrument" gives:
- with the relative layout: EUR/USD about 30 MB, gold about 58 MB (slightly over, on
  a busy day's measurement) → **1 year default**, recommended;
- with plain integers and delta encoding: gold about 97 MB → the default would have to
  be 6 months (gold about 49 MB).

**Decided (owner):** the relative layout for S5 files, default 1 year.

With 5s candles kept for a year, **ticks can expire after 30 days**: everything from
5s up is still there (charts, replay in 5-second steps), only 1s charts and
tick-by-tick replay are limited to the last 30 days.

## 10. Historical ticks from Dukascopy (optional, separate source)

OANDA's REST API has no tick history, so ticks exist only from the day the server
started recording. Dukascopy publishes free historical ticks for forex and metals.

- **Data**: one LZMA-compressed binary file (`.bi5`) per instrument per hour, each tick
  with a millisecond time, bid, ask and their volumes; history from about 2003 (gold
  from 2003-05-04 per Tickstory's list), served from a public URL without an account.
- **Tooling**: the format is simple (fixed 20-byte records after LZMA decompression),
  so a downloader is about 100 lines of Python with the standard library (`lzma`,
  `struct`), no new dependency. Existing tools: `dukascopy-node` (Node.js CLI, MIT),
  `duka` (Python), Tickstory (desktop app).
- **Size per year**: not measured (that needs downloading their files, which I did not
  do without your approval). A third-party project quotes 10-15 GB for gold's whole
  history, about 0.4-0.65 GB per year in `.bi5`; in our tick Parquet layout it would be
  of the same order. One month of downloads would give the real figure.
- **Terms of use**: Dukascopy's website terms license downloads "only for your own
  personal, non-commercial use", and say the website and its information "may not be
  used to construct a database of any kind"; redistribution needs written consent.
  There is no separate licence for the historical data. So: acceptable as an opt-in
  feature that each owner turns on for their own personal install, after reading the
  terms; **not acceptable on the hosted multi-instance setup** (the operator would be
  building and serving a database of their data), and never shipped as data with
  OpenChart.
- **Provider design**: a separate, history-only source named `dukascopy`, with its own
  symbol mapping (`XAU_USD` to `XAUUSD`, and Dukascopy's per-instrument price scale),
  implementing a small `TickHistory` interface (`fetch_ticks(instrument, hour)`), not
  the live `Provider` protocol (no stream, no candles, no account). Its ticks are
  stored under `ticks/dukascopy/...`, as every series is already keyed by source, and
  are never mixed into the OANDA series: different liquidity providers, different
  spreads and prices. The replay picks the source explicitly and the chart shows
  "DUKASCOPY" in the legend. Drawings and alerts stay on OANDA's series; a replay on
  Dukascopy data shows the OANDA drawings read-only, with a note that prices differ
  slightly between brokers.
- Code: medium (the downloader, the interface, a source choice in replay, a settings
  page with the terms). Risk: low technically; the legal question is the owner's.
- **Decided (owner):** an optional opt-in provider for personal installs only, never
  bundled, never offered on hosted instances; nothing downloaded or measured now;
  revisit with the multi-source work.

## 11. Cold storage for old data

- **What**: data older than its hot window that would otherwise be deleted: M1 beyond
  the M1 depth, S5 beyond the 5s retention, ticks beyond the tick retention. Each
  becomes an optional "archive instead of delete" next to the retention setting.
- **Format**: one compressed Parquet file per instrument and month,
  `data/cold/<source>/<instrument>/<kind>/<YYYY-MM>.parquet`, integer prices with the
  scale in the file's metadata. Measured: M1 as zstd Parquet takes about 31 bytes a
  row, about 11 MB per instrument per year (against 50 MB in SQLite today).
- **Interface**: a `ColdStore` with `months(source, instrument, kind)`,
  `read(source, instrument, kind, start, end)` and `write_month(...)`, on top of
  `pyarrow.fs` (already a dependency): `LocalFileSystem` now; an S3-compatible bucket
  later is a different filesystem object and a URL in the settings, no code change in
  the callers. No cloud service in this milestone.
- **Reading**: the series reader falls back to the cold store for a range before the
  hot data (a 1m chart scrolled past the M1 depth loads whole months from the
  archive), and the replay reads it like any other fine data. Alerts and the live
  feed never read it.
- **Moving data**: the daily retention pass writes the expired month to the archive
  (once complete), verifies it by reading it back, then deletes the rows or files.
- Code: medium. Risk: low; the hot paths are unchanged.

### Sources

- Dukascopy, Terms of Use: https://www.dukascopy.com/swiss/english/legal-pages/terms-of-use/
- Dukascopy, Historical price feed FAQ: https://www.dukascopy.com/wiki/en/faq/JForex/feed/
- Tickstory, Dukascopy history date ranges: https://tickstory.com/dukascopy-historical-data-available-date-ranges/
- dukascopy-node: https://github.com/Leo4815162342/dukascopy-node
- TickVault (size figure for gold, third party): https://github.com/keyhankamyar/TickVault
