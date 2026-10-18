# API

All REST routes are under `/api` and return JSON. Times are UTC epoch seconds. Prices are floats.

## Authentication

Every route except `/api/health`, `/api/setup/status`, `/api/setup` and `/api/login` needs the session cookie (`openchart_session`, HTTP-only, SameSite=Lax, 30 days) set by `/api/login` or `/api/setup`. Without it: `401`. The websocket closes with code `4401`.

Requests that change something (`POST`, `PUT`, `DELETE`) and websocket connections that carry an `Origin` header must come from the server's own origin, otherwise `403`. Scripts without an `Origin` header are fine.

| Route | |
|---|---|
| `GET /api/setup/status` | `{setup_required, authenticated, setup_code_required, secret_store_error}` |
| `GET /api/setup/defaults` | before setup: values already known from the environment (masked), to prefill the wizard |
| `POST /api/setup` | `{password, oanda_environment, oanda_account_id, oanda_token, telegram_token?, telegram_chat_id?, instrument, setup_code?}`. Only once (`409` afterwards). From another computer than the server, `setup_code` (printed in the server log) is required (`403`). OANDA credentials and the instrument are checked first (`400` with OANDA's message). Another OANDA environment or account than the one the stored history came from: `409`. Logs the browser in. |
| `POST /api/login` | `{password}`. `401` wrong, `429` with `Retry-After` after 5 failures from one address (or 30 in total) within 15 minutes |
| `POST /api/logout` | ends this browser's session |
| `POST /api/password` | `{current, new}`; logs out every other session |
| `GET /api/settings` | settings; secrets only as `{set, masked: "****1234", source: "saved" \| "environment" \| null}` |
| `PUT /api/settings` | any of `oanda_environment`, `oanda_account_id`, `oanda_token`, `telegram_token`, `telegram_chat_id`, `clear_telegram`, `store_ticks`, `timezone`, `listen_on_network`. Empty or missing tokens are kept. New OANDA credentials are checked before saving (`400`). Another environment or account while price history is stored: `409` with the reason (it would mix two price feeds). Returns the settings plus `restart_required` |
| `POST /api/settings/test/oanda` | `{environment?, account_id?, token?}` (missing: the saved value) → `{ok, message}` |
| `POST /api/settings/test/telegram` | `{token?, chat_id?}` → `{ok, message}`; sends a test message |
| `GET /api/instruments/available?q=` | instruments of the broker account, matching `q` |
| `POST /api/instruments` | `{instrument}`: enable it; its history downloads in the background |
| `DELETE /api/instruments/{instrument}` | disable it (stored history is kept) |

Before setup, the two test routes and `/api/setup/defaults` also work without a session, for the wizard (same setup-code rule, sent as the `X-Setup-Code` header).

## Timeframes

`1s 5s 15s 30s 1m 5m 15m 30m 1h 4h 1D 1W`

- Sub-minute bars come from stored ticks (tick storage must be on). REST serves at most the last 3 days of them.
- `1m` to `30m` come from stored M1 (default 2 years). `1h` and above also use the deep H1 history where M1 does not reach (gold on OANDA: back to March 2006).
- `1m` to `1h` align to multiples of their length.
- `4h`, `1D` and `1W` are anchored to the trading-day close, 17:00 New York: 4h bars open at 17, 21, 01, 05, 09 and 13 New York time, a daily bar runs 17:00 to 17:00, and a weekly bar starts Friday 17:00. At a DST change these bars are an hour shorter or longer, as with the broker's own candles.
- A bar's `t` is its **start**. So Monday's daily bar has `t` = Sunday 17:00 New York, as in OANDA's own data. Clients label daily bars by trading date.

## REST

| Route | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` |
| `GET /api/version` | `{"version", "api_version"}` (2 since the indicator lists live on the server) |
| `GET /api/status` | feed state: `provider`, `provider_state` (`connecting`, `connected`, `reconnecting`, `stopped`, `not_configured`, `no_instruments`), `reconnects`, `error`, and per instrument `last_tick_age` (seconds) and `history` (backfill state) |
| `GET /api/instruments` | stored instruments with `decimals`, `pip_size`, `display_name`, trading `hours`, and learned `closures` (`[[start, end], ...]`, holidays and early closes; SPEC section 9) |
| `GET /api/candles` | see below |

### `GET /api/candles`

| Parameter | Default | |
|---|---|---|
| `instrument` | required | e.g. `XAU_USD` |
| `timeframe` | `1m` | |
| `price` | `mid` | `mid`, `bid` or `ask` |
| `from` | none | first bar start to include. A bar that starts before `from` is left out, not cut in half |
| `to` | now | exclusive: bars starting before `to`, each returned whole |
| `limit` | 1000 (max 5000) | with `from`: the first `limit` bars from there; without `from`: the latest `limit` bars |
| `forming` | `true` | `false`: only what is stored (see below) |

```json
{"instrument": "XAU_USD", "timeframe": "1h", "price": "mid", "decimals": 3,
 "candles": [{"t": 1791838800, "o": 4174.4, "h": 4175.885, "l": 4166.705, "c": 4173.68,
              "v": 8104, "complete": false, "forming": true}]}
```

`v` is **tick volume** (number of price updates), not traded volume. Only the last bar can have `"complete": false` (its period is not over yet).

**The forming bar.** A minute is stored only once it has closed, so the minute that is forming is in memory (and on the websocket). The answer folds it in, for the answer only: into the last bar if that is the bar of its period, otherwise as a bar of its own at the end (the 1m chart's live bar, the first minute of a new period). That bar has `"forming": true` (the key is absent on every other bar) and is never `"complete": true`, even if its period has passed while no tick has closed the minute yet. It is never written: not to the stored minutes, not to the cache of 4h, D and W bars, so the next read, with or without `forming`, starts from what is stored. It is left out when the window ends before the live minute (`to`), when the page is full (`from` and `limit`), and for sub-minute timeframes (built from ticks, which already include it). With a connection that joined mid-minute (after a start or a reconnect) its first minute is partial, as the chart's live bar is. `forming=false` gives the stored bars only.

Errors: `400` unknown timeframe or price, `404` unknown instrument, `422` invalid parameter.

## Alerts

| Route | |
|---|---|
| `GET /api/alerts?instrument=&drawing_id=` | all alerts, or those of an instrument or a line (each with its `status`, which the line's bell shows) |
| `POST /api/alerts` | `{instrument, type, drawing_id? , level?, direction?, trigger_price?, trigger_mode?, timeframe?, frequency?, cooldown_s?, name?, expires_at?, notify?}` → the alert |
| `PUT /api/alerts/{id}` | any of the fields above, `status: "active" \| "stopped"`, `clear_expiry` |
| `DELETE /api/alerts/{id}` | delete one |
| `GET /api/alerts/events?limit=&alert_id=` | event log, newest first |

- `type`: `line_cross` (needs `drawing_id`, a line on that instrument) or `level_cross` (needs `level`).
- `direction`: `cross` (default), `up`, `down`. `trigger_price`: `bid`, `ask`, `mid` (default). `trigger_mode`: `touch` (every tick, default) or `candle_close` (the bar's close at the close time). `timeframe`: the bar for `once_per_bar` and `candle_close` (`1m` or more). `frequency`: `once` (default), `once_per_bar`, `repeating` (with `cooldown_s`). `notify`: `["telegram", "app"]` (default).
- `status`: `active`, `triggered` (a `once` alert fired), `expired`, `stopped`. Saving an alert as `active` or changing its condition re-arms it (it needs one fresh price first).
- An event: `{id, alert_id, ts, price, line_value, cross, message, late, detected_at, delivered, attempts, error}`. `late` is `wake`, `reconnect` or `restart` when the cross was found from stored history after missed time; `ts` is when it happened, `detected_at` when the server saw it.
- Deleting a line deletes its alerts; deleting an alert leaves its line.

## Indicators

| Route | |
|---|---|
| `GET /api/indicators` | `{indicators, errors, folder, builtin, custom, isolation, worker, editing}` (below) |
| `GET /api/indicators/{id}?instrument=&timeframe=&price=&params=&kind=&from=&to=&limit=` | `{id, kind, name, instrument, timeframe, price, params, t, values, hits, levels}`, plus `checks` for your own files. `kind` is `builtin` or `custom` and picks the namespace (without it a built-in wins and a file of yours is found when no built-in has the id; anything else is a 400) |

- **The catalog answer.** `builtin` and `custom` are separate namespaces: a file of yours named `ema.py` is another indicator than the built-in EMA, and every entry, section and error says its `kind` (`builtin` or `custom`).
  - `indicators`: the flat list the menu reads, built-in ones then yours that run (a file of yours with a built-in's id is only in `custom`), each with `id, name, title, overlay, params, outputs, builtin` and `kind` (`builtin` or `custom`);
  - `errors`: files that did not load or run (`{file, error, kind}`);
  - `folder`: the folder for your own (docs/indicators.md);
  - `builtin`: every built-in indicator (same entries as above);
  - `custom`: one entry per file, `{id, kind: "custom", file, status, reason, line, definition, checks}`; `status` is `ok`, `error`, `quarantined` or `not_run` (the worker is not available); `definition` is the entry as in `indicators` (null if it did not load); `checks` is the save-time checks of the current version (below);
  - `isolation`: how this install isolates your files, `{level, minimum_met, reason, notice, probes, notes}` (docs/custom-indicators.md); `{level: "starting"}` until the worker has started;
  - `worker`: `{state, level, restarts, error, start_seconds, pid}`, or null before there is one;
  - `editing`: whether code may be edited in the browser from this request, `{allowed, setting, reason}` (docs/custom-indicators.md).
- A parameter: `{name, type, default, label, group, min?, max?, step?, max_length?, choices?}`; `type` is `int`, `float`, `bool`, `choice`, `string`, `source`, `color` or `width`; `group` is `inputs` or `style` (the settings tabs). `max_length` is for `string` (1 to 1000, default 100).
- **An output** always has `kind`, `name` and `label`; the rest depends on the kind. `color` and `width` name parameters (the browser reads the value from `params`); `width` is `null` where the output has none. Line styles are `solid`, `dashed`, `dotted`.

  | `kind` | other fields | values in the answer |
  |---|---|---|
  | `line` | `color, width, style` | `values[name]`: a number or `null` per bar |
  | `histogram` | `color, base` | `values[name]`: a number or `null` per bar; columns from `base` |
  | `area` | `color, width` | `values[name]`: a number or `null` per bar; shaded below the line |
  | `fill` | `between: [upper, lower], color, opacity` | none; shades between the `values` of two outputs. `name` is `fill:1`, `fill:2`, ... |
  | `level` | `value` (number or `null`), `input` (a Float input's name or `null`), `color, width, style` | `levels[name]`: the number to draw at. `name` is `level:1`, ... |
  | `marker` | `color, shape, position, text` | `hits[name]`: where it is drawn |
  | `background` | `color, opacity` | `hits[name]`: the bars to tint |

  `shape` is `arrow_up`, `arrow_down`, `circle` or `square`; `position` is `above`, `below` or `on` (the bar's close); `opacity` is above 0 and at most 1. M7 outputs (`{name, label, color, width}`) are `line` with `style: "solid"`; the old fields are unchanged.
- **Values:** `params` is a JSON object (missing ones take their defaults; unknown or invalid ones are a 400). The bars are those `GET /api/candles` returns for the same `instrument, timeframe, price, from, to, limit`, including the bar that is forming (see `forming` there): the values have a value for it, and it moves with each tick. `t` holds the start times. Earlier bars are read as the indicator's warm-up needs, so values do not depend on where the window starts.
  - `values`: one list per `line`, `histogram` and `area` output, as long as `t`, `null` where there is no value (a gap, or not a finite number).
  - `hits`: for each `marker` and `background` output, the ascending list of positions **in `t`** (0-based) where it is on. `[]` when never. A thousand bars with a marker every tenth bar is 100 integers, not 1,000 booleans.
  - `levels`: `{name: number}` for each `level`, already resolved with this request's `params`.
  - Every key of `values`, `hits` and `levels` is the `name` of an output; `fill` outputs have none (the browser draws them from the two outputs they name).
  - 404 for an unknown indicator or instrument, 400 for bad `params`. **422 when the indicator's code fails or returns something that cannot be drawn** (a missing or undeclared output, the wrong number of values, text in a line, numbers in a marker, ...): `{"detail": "SMA cross failed: ResultError: output 'up' must be true/false values ..."}`. The server checks every answer itself, also those of the worker. A bad result is not counted toward the quarantine (it is not a crash or a timeout), and the worker keeps running; the cost of repeating one is bounded by the job deadline and by the server remembering the same error for the same script, parameters and bars.
  - **Computed values are cached in the server** (CI-5): the same indicator version, inputs and bars are answered without computing again; inputs of the Style tab (colours, widths) are not part of that, so a restyle never recomputes. A window that ends on the forming bar is computed at most once a second and in between answered with the last result for that window (the newest tick can be up to a second late there); a new bar is computed at once. Identical requests that arrive together share one computation.
  - **A marker or background is only checked to be 0 or 1** on the server (booleans reach it as numbers); the worker's own check that the script returned real `True/False` is the only one that can tell them apart.
- **Checks (your own files only):** the first request for a version of a file with given `params` queues the save-time checks on its bars (docs/custom-indicators.md). They never hold up or block the answer. `checks` in the values answer is the check for these `params`; each entry of `custom` in `GET /api/indicators` has `checks`, the list for its current version (at most 5 settings). Built-in indicators have no `checks`.

  ```json
  {
    "version": "3f2a9c1d4e5b", "params": {"length": 20, "color": "#2962ff", "width": 1},
    "instrument": "XAU_USD", "timeframe": "5m", "price": "mid",
    "bars": 320, "declared_warmup": 20,
    "lookahead": "warned", "warmup": "ok",
    "findings": [
      {"check": "lookahead", "level": "warning", "output": "x", "t": 1760535900,
       "full": 2005.12, "other": null,
       "message": "'x' changes when the last 50 bars are removed: on this bar it is 2005.12 with all 320 bars and a gap with 270. The script reads later bars, or does not give the same result twice (random numbers, the clock)."}
    ]
  }
  ```

  - `version`: the first 12 hex digits of the file's SHA-256. `bars`: how many bars were checked (the request's, with its warm-up bars).
  - `lookahead`, `warmup`: `pending`, `ok`, `warned`, `skipped`, `timeout`, `failed`, or `not_checked` (with a `message`: this version was already checked with 5 other settings, or too many checks are waiting).
  - **A finding:** `check` (`lookahead` or `warmup`), `level` (`warning`, or `note` for what is not a problem with the script), `output` (an output's name, or `null` for a note about the check itself), `t` (the start time of the first bar that differs, or `null`), `full` (the value there with all the bars), `other` (the value with fewer bars, or with less history), `message`. Values are numbers, `null` for a gap, 0/1 for markers and backgrounds. A warmup warning also has `needed`: the warmup that would have been enough in this check, or `null` when no warmup can fix it.
  - One finding per output and check, at its first differing bar.

## A chart's indicators

A chart is a source and an instrument (the source is `provider` in `GET /api/status`). Its indicators are kept on the server, so they are the same in every browser and on the phone. These routes need the login, not the custom-indicator editing setting.

| Route | |
|---|---|
| `GET /api/charts/{source}/{instrument}/indicators` | `{source, instrument, revision, indicators}` |
| `PUT /api/charts/{source}/{instrument}/indicators` | `{indicators, revision?}`: the whole list, in legend order → the chart as `GET` answers it |
| `POST /api/charts/{source}/{instrument}/indicators/import` | `{indicators}`: a browser's list from before (its `localStorage`) → the chart plus `skipped` and `imported` |

- **An instance:** `{uid, kind, id, params, visible, price_labels, legend_values}`.
  - `uid`: the instance's stable id (letters, digits, `_` and `-`, at most 40); a new one is given when it is left out.
  - `kind`: `builtin` or `custom`. Left out, it is the built-in with that `id`, or the custom one when only a file of yours has it (how older lists are read).
  - `params`: at most 64 values, each `true`/`false`, a finite number or a text of at most 1000 characters, 8 KB of JSON in all. `visible`, `price_labels` and `legend_values` are `true` or `false` (no other value is read as one) and default to `true`.
  - **Answers add** `status` (`ok`; `missing` when no definition has that id in that kind; or a custom file's `error`, `quarantined`, `not_run`) and `reset`, the inputs whose values no longer fitted (below).
- **`revision`** is 0 for a chart whose list was never set and goes up by one with every write. A `PUT` that gives `revision` (a whole number, not `true` or text) is refused with `409` `{detail: {message, chart}}` if the chart has moved on since.
- **A refused body is a `422`** with FastAPI's usual `detail` list; a `NaN` or an infinity in the input it echoes is sent as text (`"nan"`), since JSON cannot carry it (that was a 500 before, for every route).
- **Validation:** `PUT` stores the values complete (every input, defaults for those not given). An unknown definition, a `uid` used twice or a value its input does not accept is a `400`; values outside the bounds above, NaN or infinities are a `422`. A value for an input the definition does not have (removed, or never there) is dropped and listed in `reset`. An instance of a file of yours that does not load at the moment keeps its values as given; they are checked once it loads again.
- **"Settings reset":** when the definition was edited after the values were stored (an input removed, a choice renamed, a range narrowed), `GET` answers those inputs with their defaults and lists them in `reset`, without changing what is stored (editing the file back brings the values back). The next write stores the list as it now fits; its answer and broadcast carry the note once.
- **Import:** accepted only for a chart whose list was never set (`revision` 0), also not after it was set to an empty list: `409` `{detail: {message, chart}}` otherwise. **A list that brings nothing** (empty, or nothing usable in it) changes nothing: `200` with `imported: false` and `revision` still 0, so another browser's real list can still come. The list is read entry by entry (at most 200): entries that are not an instance, hold a value that cannot be kept, have no definition any more, or come after the first 30 are left out and listed in `skipped` (`[{id, kind, reason}]`), the rest is imported; values that no longer fit fall back to their defaults with the note.
- Every change is sent to all connected browsers as a websocket `chart_indicators_changed` message.

## Your indicator definitions

Everything under `/api/custom-indicators` answers **`403`** `{detail: "Editing custom indicators is off: <reason>."}` unless editing is allowed for this request: the minimum isolation (AppContainer or sidecar) **and** the server's `indicator-editing` setting. `editing` in the catalog (`{allowed, setting, reason}`) says which, so the browser can show the menu items disabled with the reason instead of trying. Files placed in the folder by hand run either way.

**Status:** `usage` and `DELETE` exist (CI-3). The rest of this section is the **CI-4 contract**, written before the code so the editor can be built against it; until CI-4 lands those routes answer 404.

| Route | |
|---|---|
| `GET /api/custom-indicators/templates` | `[{id, title, description}]`: what New can start from |
| `POST /api/custom-indicators` | New `{id, template, title?}` or Import `{source, id?, filename?}` → `201` the definition |
| `GET /api/custom-indicators/{id}/source` | `{id, file, version, source, bytes, updated_at}`; `?download=true`: the file itself (Export) |
| `PUT /api/custom-indicators/{id}/source` | Save `{source, base_version?}` → the definition |
| `PATCH /api/custom-indicators/{id}` | Rename `{title}` → the definition |
| `POST /api/custom-indicators/{id}/duplicate` | `{id?, title?}` → `201` the new definition |
| `GET /api/custom-indicators/{id}/versions` | `[{version, saved_at, bytes, digest}]`, newest first, at most 10 |
| `GET /api/custom-indicators/{id}/versions/{version}` | `{id, version, source, saved_at}` |
| `POST /api/custom-indicators/{id}/versions/{version}/restore` | puts that version back (a save) → the definition |
| `POST /api/custom-indicators/preview` | Run: unsaved `source` on a chart query → values, nothing stored |
| `GET /api/custom-indicators/{id}/usage` | `{id, in_use, charts: [{source, instrument, instances}], layouts: [{id, name, instances}]}` |
| `DELETE /api/custom-indicators/{id}` | moves `<id>.py` and `.history/<id>/` to `.deleted/<id>-<UTC time>/` (as `<id>.py` and `history/`) → `{id, moved_to, removed}` |

**Ids, files and sources (every route):**

- **`id`** is the file name without `.py`: `[a-z][a-z0-9_]{0,39}`, the whole string (no trailing newline, no capitals, no `.`, `/` or `\`). Anything else is a `404` in a path and a `400` in a body. It is fixed when the file is created; Rename changes the title only. A custom id may equal a built-in's (`ema`): the two are separate namespaces.
- **Only `<id>.py` directly in the indicators folder** is read or written. A name that is a link, a junction, a folder or anything but a plain file is a `409` and is never followed or replaced. Nothing is written outside the folder, `.history/` and `.deleted/`.
- **`source`** is text, at most **256 KB** as UTF-8 (`413` above that). A leading byte-order mark is dropped; line endings are kept as sent. A NUL character, or text that is not valid Unicode (a lone surrogate such as `"\ud800"`), is a `400`.
- **Writes are atomic:** the new text goes to a temporary file in the folder, then replaces `<id>.py` in one rename, so a reader (or a crash) sees the old file or the new one, never half of one. Writes to one id are serialized.
- **Saved even with errors.** A file that does not load (a syntax error, a schema error) is still stored, so no work is lost; its problem is in the answer.
- **`version`**: the first 12 hex digits of the source's SHA-256 (as `checks[].version`). **A history version** is the UTC time it was saved, `YYYYMMDDTHHMMSSZ`, with `-2`, `-3` ... when two land in the same second.

**The definition** (the answer of New, Import, Save, Rename, Duplicate and Restore) is the definition's entry in the catalog's `custom` list, plus:

```json
{"id": "sma_cross", "kind": "custom", "file": "sma_cross.py", "status": "error",
 "reason": "SyntaxError: invalid syntax", "line": 23, "definition": null, "checks": [],
 "version": "3f2a9c1d4e5b", "saved": true, "unchanged": false,
 "error": {"type": "SyntaxError", "message": "invalid syntax", "line": 23, "traceback": "  line 23\n    up = ta.crossover(fast, slow) /"}}
```

- `status`, `reason`, `line` and `definition` as in the catalog (`definition` is `null` while the file does not load). `error` is `null` when it loads. `checks` starts empty: the lookahead and warmup checks run on the first chart requests for this version, as for any file.
- `unchanged`: `true` when the source sent is the one already stored (no new history version, no broadcast).
- **Every write is broadcast** as `indicators_changed` `{id, op}` (`op`: `create`, `save`, `rename`, `duplicate`, `restore`, `delete`); pages fetch the catalog again, and charts using the id fetch their values again (the new version is a new script).

**Each route:**

- **Templates** ship with OpenChart: `blank` (one line, the smallest file that loads) and the four examples of docs/custom-indicators.md (`sma_cross`, `rsi_bands`, `bollinger_fill`, `engulfing`). Each writes `TITLE = "..."` on one line.
- **New** `{id, template, title?}`: copies the template under `id`, with `title` (1 to 60 characters, default the template's) on its `TITLE` line. `409` `{detail: {message, suggested}}` if `<id>.py` exists, `suggested` being the first free `<id>_2`, `<id>_3` ...; `400` for an unknown template.
- **Import** `{source, id?, filename?}`: stores `source` as is. Without `id`, it is made from `filename` (`My RSI.py` → `my_rsi`: lowercased, other characters to `_`, a leading digit or `_` gets an `x`, cut to 40) or is `imported`. `409` with `suggested` if it exists: nothing is ever overwritten by an import.
- **Source / Export:** `?download=true` answers the file as `text/x-python; charset=utf-8` with `Content-Disposition: attachment; filename="<id>.py"`.
- **Save** `{source, base_version?}`: `404` if the file does not exist (Save never creates; use New or Import). The file as it was goes into `.history/<id>/<version>.py` first; only the last 10 are kept, the oldest dropped. **`base_version`** is the `version` the editor started from: if the stored file has changed since (another tab, a text editor), the save is refused with `409` `{detail: {message, version}}` and nothing changes. Without it, the save always applies.
- **Rename** `{title}`: rewrites the one `TITLE = "..."` line (a save, with its history version). `409` `{detail: {message: "edit TITLE in the code"}}` if the file has no such line, or more than one, or it spans lines. `title`: 1 to 60 characters, no line break.
- **Duplicate** `{id?, title?}`: copies the current file (not its history) to `id`, default the first free `<id>_2` ...; `title` default `"<title> (copy)"`, written on the copy's `TITLE` line when it has one (else the copy keeps its code as it is). `409` with `suggested` if `id` exists.
- **Versions:** the list is read from `.history/<id>/`; `404` for a version that is not there. **Restore** is a save of that version's text (so the current file goes into the history first, and a restore can itself be undone); `base_version` may be given as for Save.
- **Preview (Run)** `{source, id?, instrument, timeframe, price?, params?, from?, to?, limit?}`: the query is that of `GET /api/indicators/{id}` (same bars, same warmup, the forming bar included). Runs `source` in the worker without storing anything (no file, no history, no catalog change, no quarantine count, no save-time checks). `id`, if given, names the file in tracebacks (`<id>.py`), else `preview.py`.
  - The answer is the values answer (`t, values, hits, levels, params`) plus `definition` (the schema the source declares) and `preview: true`.
  - `422` `{detail: {type, message, line, traceback}}` when the source does not load, its `params` do not fit, it raises, times out (`type: "timeout"`) or returns something that cannot be drawn; `503` when the worker is not running; `400` and `404` as for values.
  - One preview at a time per browser tab is enough for the editor; the server runs previews like chart requests, under the job deadline.
- **Usage, Delete:** as below.

**Usage and delete (CI-3):**

- `404` for an id that is not one of your files.
- **In use:** `DELETE` answers `409` `{detail: {message, usage}}`, the message as the confirmation shows it (`"SMA cross" is on 2 charts (EUR_USD, XAU_USD) and in 1 saved layout ("Gold H1"). Deleting it removes it from all of them.`). `?confirm=true` removes its instances from every chart and saved layout first (`chart_indicators_changed` for each chart, `layouts_changed`), then moves the files. Only custom instances count and go: a built-in indicator with the same id is untouched, and a layout entry saved without a `kind` counts only when no built-in has that id.
- `409` without any change when `.deleted` or `.history/<id>` is not a plain folder inside yours (a file, or a link elsewhere).
- After a delete, every page gets `indicators_changed` `{id, op: "delete"}` (fetch the catalog again).

**Errors, all routes:** `400` a malformed body or id; `403` editing not allowed (the reason in `detail`); `404` no such file, version or template; `409` the id exists (`suggested`), the file changed since `base_version` (`version`), a name that is not a plain file, a `TITLE` line Rename cannot edit, a definition in use (`usage`); `413` a source over 256 KB; `422` a preview that fails, or a refused body (FastAPI's `detail` list); `503` the worker is not running (preview only: saves never need the worker to store the file).

**Errors, all routes:** `400` a malformed body or id; `403` editing not allowed (the reason in `detail`); `404` no such file, version or template; `409` the id exists (`suggested`), the file changed since `base_version` (`version`), a name that is not a plain file, a `TITLE` line Rename cannot edit, a definition in use (`usage`); `413` a source over 256 KB; `422` a preview that fails, or a refused body (FastAPI's `detail` list); `503` the worker is not running (preview only: saves never need the worker to store the file).

## Warmup notice

`GET /api/indicators/{id}` fetches at most `20000` bars before the first one shown (`MAX_WARMUP`). When the settings need more (the indicator's `warmup(params)` is above it: a built-in EMA of length 2000 or more, a custom file that declares a large warmup), the values are still returned, computed on the bars that were fetched, and the answer says so:

- **`notice`**, only when the warmup was cut: `{"kind": "warmup_cut", "needed": 20010, "limit": 20000, "message": "..."}`. `needed` is the declared warmup in bars, `limit` what the server uses; `message` is for people. The chart shows it as a small "Warmup cut" warning on the indicator's legend row.
- **`checks.warmup_cut`**, custom files only: the same object, added to the answer's copy of `checks` (the save-time results kept for the file's version do not contain it).

An answer whose warmup fits has neither. Clients that ignore the fields keep working.

## Layouts

| Route | |
|---|---|
| `GET /api/layouts` | `[{id, name, updated_at, default}]`, by name |
| `GET /api/layouts/{id}` | the same plus `layout` |
| `POST /api/layouts` | `{name, layout, default?}` → the layout |
| `PUT /api/layouts/{id}` | any of `name`, `layout`, `default` (`true` makes it the one that loads on start, `false` unsets it) |
| `POST /api/layouts/{id}/apply?instrument=` | put the layout's indicators on a chart (the layout's instrument unless `instrument` is given) → `{layout, chart, skipped}` |
| `DELETE /api/layouts/{id}` | delete it (and unset the default if it was) |

- `layout`: `{instrument, timeframe, chart_type, price, theme, panel_open, panel_view, indicators}`; `chart_type` is `candles`, `bars`, `line`, `area` or `heikin_ashi`. At most one layout is the default.
- **`indicators`** is a copy of a chart's instances (`{uid, kind, id, params, visible, price_labels, legend_values}`, as above). **Left out, the layout copies its instrument's list from the server**; given (older clients did this), it is checked like a `PUT` to a chart and stored complete, with a `kind` and a `uid` added to each entry.
- **`apply`** replaces the chart's list with the copy (the same `uid`s), broadcast as `chart_indicators_changed`. It reads the copy as the import reads a browser's list: values that no longer fit fall back with the `reset` note; entries whose definition is gone (or that cannot be used) are left out and listed in `skipped`. `400` when neither the layout nor the request names an instrument.

## Drawings

| Route | |
|---|---|
| `GET /api/drawings?instrument=` | drawings (all instruments if omitted) |
| `POST /api/drawings` | `{instrument, type, points, style?, label?}` → the drawing with its `id` |
| `PUT /api/drawings/{id}` | any of `points`, `style` (partial), `label`, `clear_label` |
| `DELETE /api/drawings/{id}` | delete one |
| `DELETE /api/drawings?instrument=` | delete all on an instrument → `{deleted}` |

- `type`: `hline` (1 point), `trendline`, `ray`, `extended` (2 points each).
- `points`: `[{t, p}]`, `t` in UTC epoch seconds, `p` a price. The two points of a line need trading time between them (`400` otherwise).
- `style`: `{color: "#rrggbb", width: 1-4, dash: "solid" | "dashed" | "dotted"}`; `label` up to 100 characters.
- Every change is sent to all connected browsers as a websocket `drawing_changed` message.
- Line geometry (how `points` define the line, in trading time): SPEC section 9.

### More drawing tools

Added with `docs/decisions-drawing-tools.md` (this section extends the `type`, `points` and `style` rules above). No migration; the `drawings` table, the routes and `drawing_changed` are the same.

| `type` | `points` | Valid when | Notes |
|---|---|---|---|
| `vline` | 1 | always | `t` is the line; `p` is kept but not drawn |
| `text` | 1 | `label` has 1-100 characters after trimming | the text is `label`; `clear_label` and a blank `label` are refused (`400`); `style.width` 1-4 is the size |
| `rect` | 2 opposite corners | trading time between them and different prices | |
| `fib` | 2: start (level 1), end (level 0) | trading time between them and different prices | levels 0, 0.236, 0.382, 0.5, 0.618, 0.786, 1 are fixed |
| `measure` | 2 | the points differ in trading time or in price | |
| `channel` | 3: base line (2), then a point on the parallel line | trading time between the first two, and the third not on the base line | the base line is extended in trading time; the offset of the third point from it is the channel's width |

- `style` also takes `fill` (integer 0-100, opacity of the area of a `rect` or `channel`) and `extend` (boolean: a `channel` runs to the right as rays, a `fib`'s levels to the edge). Both are stored only when sent; without them the tool's default applies.
- `style` also takes `locked` and `hidden` (booleans, stored only when sent). **Locks guard against accidents in this browser:** the web page will not move or delete a locked drawing, but the server stores the flag and does not enforce it, so `PUT` and `DELETE` on a locked drawing work. A hidden drawing is not drawn, and its alerts stay active.
- A `line_cross` alert needs a `hline`, `trendline`, `ray` or `extended` line: on any other type `POST /api/alerts` answers `400`.
- A change that breaks a rule is refused with `400` on create and on update, like the two-point lines.

## WebSocket `/ws`

JSON messages, each with a `type`.

On connect the server sends `hello` `{api_version, server_time}` and then `status`. Clients should warn if their `api_version` is older than the server's. The web page also warns when the server's is older than its own, since an older server has no chart indicator routes.

### Client to server

| Message | |
|---|---|
| `{"type": "subscribe", "instrument": "XAU_USD", "timeframe": "5m", "price": "mid"}` | `price` is optional (default `mid`). Answered with `subscribed` |
| `{"type": "unsubscribe", ...same fields}` | answered with `unsubscribed` |
| `{"type": "ping"}` | answered with `pong` `{server_time}` |

### Server to client

| Message | When |
|---|---|
| `tick` `{instrument, t, bid, ask}` | every price update for an instrument the client subscribed to (any timeframe) |
| `candle` `{instrument, timeframe, price, candle: {t, o, h, l, c, v}, closed}` | on every tick (`closed: false`, the forming bar) and once when the bar ends (`closed: true`). A subscription immediately receives the current forming bar if there is one |
| `status` | every 5 s and on feed state changes, same body as `GET /api/status` |
| `drawing_changed` `{op: create\|update\|delete, drawing}` | any drawing changed, from any browser |
| `alert_changed` `{op, alert}` | an alert was created, edited, stopped, expired or deleted |
| `alert_triggered` `{alert, event}` | an alert fired |
| `alert_event` `{event}` | the delivery state of an event changed |
| `layouts_changed` `{layouts}` | a layout was saved, renamed, deleted or made the default, or lost a deleted indicator: the list, as `GET /api/layouts` |
| `chart_indicators_changed` `{source, instrument, revision, indicators}` | a chart's indicators changed (a `PUT`, an import, a layout applied, a definition deleted): the chart as `GET /api/charts/.../indicators` answers it, with this write's `reset` notes |
| `indicators_changed` `{id, op}` | the catalog changed: a definition of yours was created, saved, renamed, duplicated, restored or deleted (`op`; CI-3's delete sends `{type}` only until CI-4). Fetch `GET /api/indicators` again |
| `closures` `{instrument, closures}` | learned market closures changed (a holiday passed): `[[start, end], ...]` in UTC epoch seconds |
| `error` `{error}` | invalid message |

A tick is always sent before the candle update it causes.

A live bar for `1m` and above starts from stored history: subscribing to `4h` mid-period gives the whole 4h bar so far, not just the ticks since subscribing. It uses the same arithmetic as `/api/candles`, so live and historical bars agree.

**Reconnecting:** after a dropped connection, fetch `/api/candles?from=<t of your last bar>` and resubscribe; the bar at `from` is included and replaces yours. A client that falls 2000 messages behind is disconnected (close code 1013) and should do the same.
