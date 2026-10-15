# OpenChart: notes for Claude Code

OpenChart is a self-hosted charting and alerting server for forex and metals. It runs on
the owner's always-on Windows PC (or in Docker), pulls prices from the owner's own OANDA
account, draws charts in the browser, and sends alerts on drawn lines (sloped trendlines
included) to Telegram even when no chart is open. `SPEC.md` is the specification,
`docs/decisions.md` the decision log (read the latest sections first), `docs/roadmap.md`
what is done, parked and queued. Local setup and paths: `LOCAL-NOTES.md` (git-ignored,
only on the owner's PC).

## Architecture in ten lines

1. `providers/`: the broker interface (`base.py`), OANDA over REST and its price stream, and a fake broker for tests.
2. `market/feed.py`: history on start (newest first, resumable), the tick stream, live candles; replaces tick-built minutes with the broker's own candles (the last 15 minutes once a minute, the hour before a stop or sleep, the previous day daily).
3. `store/`: SQLite in WAL mode with numbered migrations (`store/migrations/NNNN_*.sql`, never edited once applied); candles as scaled integers keyed by a series id; ticks as Parquet files.
4. `market/aggregate.py`, `series.py`, `cache.py`: every timeframe from 1s to 1W built from M1/H1, 4h/D/W on the 17:00 New York trading day, complete bars cached.
5. `alerts/`: the alert engine runs in the server on exactly the drawn line (trading-time geometry), catches up after sleep or outages, notifies Telegram and browsers.
6. `api/`: FastAPI REST and one WebSocket (`docs/api.md`), single-password login (argon2, session cookie), same-origin check on writes.
7. `indicators/`: one Python file per indicator (`docs/indicators.md`); built-ins in the package, the owner's in `<data dir>/indicators/`.
8. `server.py`: wires it together; holds the data-folder lock (`lock.py`) while it runs; `main.py` is the `openchart` command line.
9. `frontend/`: TypeScript + Vite + Lightweight Charts, built into `src/openchart/web/`.
10. `packaging/`: PyInstaller build, the Windows service (NSSM, LocalSystem, tokens in an encrypted file), the desktop viewer; `Dockerfile` and `docker-compose.yml`.

## Running the checks

```bash
python -m pytest -q -m "not live"          # all tests; "live" ones call the real OANDA API: never without the owner's go
ruff check . && ruff format --check .      # lint (CI runs both)
cd frontend && npm ci && npm run typecheck && npm test && npm run build   # Node 24
```

Tests use the fake broker (`providers/fake.py`) and the OANDA HTTP mock (`tests/oanda_mock.py`,
which enforces OANDA's parameter rules). CI (`.github/workflows/ci.yml`) also builds the
Windows release, installs and reinstalls the service on the runner, and runs the Docker
image. Python 3.11 and 3.13 are both tested.

## The owner's standing rules

- **Tell the owner before any API request** to OANDA or Telegram, with the exact request and the number of requests. Keep it to one script run.
- **Never touch the owner's live data folder, `openchart.exe` or `.env`** without explicit permission for that one operation. Work on copies of the data.
- **Never push or merge without the owner's go.** One branch per piece of work, from `main`; the owner reviews and merges the PRs. Never tag or publish a release.
- **Use the fake broker and copies of data for tests**, never the owner's live server or price streams (OANDA practice allows only about two streams per account).
- **Stop for the owner's review after each milestone** or step, and wait.
- **Secrets only through an environment variable for one process**, after telling the owner. Never copy `.env`, never print or log a token, never ask for one in chat.
- Scan for secrets before every push. Screenshots only of OpenChart, never showing an account ID, a token or a chat ID. `reference/` is the owner's and is never published.
- Never register or start a Windows service on the owner's PC without asking first.

## Conventions

- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR descriptions with the Claude Code line.
- Write decisions and findings into `docs/decisions.md` as part of the change.
- New migrations take the next free number at merge time.
- Tests go with every fix, written to fail without it.
- On this Windows PC, shell heredocs mangle backslashes: edit files with the editor tools, not `sed` or inline Python with backslashes.
