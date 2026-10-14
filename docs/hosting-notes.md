# Hosting notes

## Disk and memory per instance

Measured on 2026-10-14 with XAU_USD and EUR_USD (see [storage-review.md](storage-review.md)).
"Proposed" means after the storage proposals there (compact candle format, tick
retention, M1 depth per instrument, log limits); "today" is the current format.

### Disk per instrument

| Part | Today | Proposed |
|---|---:|---:|
| H1 (deep, back to 2002-2006) | 17-19 MB, +0.8 MB/year | 6 MB |
| Cache (4h, D, W) | 6 MB | 2 MB |
| M1, per year of depth | 50 MB, and it keeps growing | 17 MB, a fixed window |
| Ticks, per trading day kept | 0.8-1.0 MB, kept forever | 0.4-0.5 MB, 30 days by default |
| 5s candles (planned), per year kept | | gold about 58 MB, EUR/USD about 30 MB; 1 year by default |
| Archive of expired data (optional, planned) | | M1 about 11 MB per year archived |

Plus per instance: logs (today without limit, about 15 MB a day while a browser shows
a chart with an indicator; proposed at most about 60 MB), backups (each about 25% of
the database when zipped: 276 MB gave 69 MB; keep a few), and SQLite's WAL (about 4 MB).

Examples for one instance:

| Setup | Today (first year) | Proposed |
|---|---:|---:|
| 1 instrument, 1 year of M1, 30 days of ticks | about 100 MB, +250 MB/year | about 40 MB, stable |
| 2 instruments, 2 years of M1 (the owner's) | about 250 MB, +600 MB/year | about 110 MB, stable |
| 5 instruments, 1 year of M1, 30 days of ticks | about 370 MB, +1.4 GB/year | about 180 MB, stable |

With 5s candles kept a year (planned), add about 60 MB (gold) or 30 MB (EUR/USD) per instrument to the proposed figures. Add up to about 60 MB of logs per instance once they are limited. "Stable" means the
size stays flat once the windows are full; only H1 keeps growing, by under 1 MB per
instrument per year.

### Memory

Measured on the copy, the server without a broker connection: about 100 MB after
start, about 110 MB after loading every timeframe of both instruments, with a peak of
230 MB while 1h bars are built from two years of M1. The live feed adds little (ticks
are written once a minute). Plan **about 150 MB per instance in steady state and a
limit of 384 MB** to absorb peaks (first download, cache rebuild). Twenty instances
fit in 4 GB of RAM with room for the operating system and a reverse proxy.

CPU is negligible at rest; a first download of an instrument is network-bound (about
200 requests to OANDA).

## Why not a cloud database

Hosted databases such as Firebase (Firestore or the Realtime Database) were
considered for candles and rejected:
- **Write limits and cost:** every closed M1 bar is a write, plus the once-a-minute
  refresh of the last 15 minutes; a first download is hundreds of thousands of rows.
  Free tiers allow about 20,000 writes a day, and paid writes are billed one by one.
- **Billed per read:** a chart of 5,000 bars reads 5,000 documents. Opening a few
  charts a day costs more than the whole server.
- **Poor fit for time series:** no range scans over packed rows, no aggregation into
  4h or daily bars on the server side, one document per bar with field names repeated
  in every one, so far more storage than the compact SQLite rows.
- **An extra dependency for self-hosters:** an account, keys and network access to a
  third party, for something SQLite does in one local file with no service to run.

The future option for **cold data** (old M1 and ticks) is Parquet files, which are
already the tick format: zstd Parquet holds M1 in about 31 bytes a row, and the files
can live on any S3-compatible object storage (cheap, no per-row billing), read in
whole months when someone scrolls that far back. SQLite would keep the recent window
and everything the alerts need.

## Data that must not be hosted for others

Historical ticks from Dukascopy (an optional, planned source, see storage-review.md
section 10) are licensed for personal, non-commercial use only, and Dukascopy's terms
forbid using its data to build a database. An operator hosting instances for other
people must leave that source off. It would also cost about 0.4-0.65 GB per instrument
per year (third-party figure, unmeasured).

## Cold storage

Old data (M1 beyond the M1 depth, 5s candles and ticks beyond their retention) can go
to monthly Parquet archives instead of being deleted (planned, storage-review.md
section 11). They live in the data folder at first; the same code can later point at
an S3-compatible bucket, which suits many instances on one server: the archives are
written once and rarely read.
