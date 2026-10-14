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
