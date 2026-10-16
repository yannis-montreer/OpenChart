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
| `GET /api/version` | `{"version", "api_version"}` |
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

- **The catalog answer:**
  - `indicators`: the flat list the menu reads, built-in ones then yours that run, each with `id, name, title, overlay, params, outputs, builtin` and `kind` (`builtin` or `custom`);
  - `errors`: files that did not load or run (`{file, error}`);
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

## Layouts

| Route | |
|---|---|
| `GET /api/layouts` | `[{id, name, updated_at, default}]`, by name |
| `GET /api/layouts/{id}` | the same plus `layout` |
| `POST /api/layouts` | `{name, layout, default?}` → the layout |
| `PUT /api/layouts/{id}` | any of `name`, `layout`, `default` (`true` makes it the one that loads on start, `false` unsets it) |
| `DELETE /api/layouts/{id}` | delete it (and unset the default if it was) |

- `layout`: `{instrument, timeframe, chart_type, price, theme, panel_open, panel_view, indicators}`; `chart_type` is `candles`, `bars`, `line`, `area` or `heikin_ashi`; each indicator is `{id, params, visible, price_labels, legend_values}`. Indicator parameters are validated and stored complete. At most one layout is the default.

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

## WebSocket `/ws`

JSON messages, each with a `type`.

On connect the server sends `hello` `{api_version, server_time}` and then `status`. Clients should warn if their `api_version` is older than the server's.

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
| `layouts_changed` `{layouts}` | a layout was saved, renamed, deleted or made the default: the list, as `GET /api/layouts` |
| `closures` `{instrument, closures}` | learned market closures changed (a holiday passed): `[[start, end], ...]` in UTC epoch seconds |
| `error` `{error}` | invalid message |

A tick is always sent before the candle update it causes.

A live bar for `1m` and above starts from stored history: subscribing to `4h` mid-period gives the whole 4h bar so far, not just the ticks since subscribing. It uses the same arithmetic as `/api/candles`, so live and historical bars agree.

**Reconnecting:** after a dropped connection, fetch `/api/candles?from=<t of your last bar>` and resubscribe; the bar at `from` is included and replaces yours. A client that falls 2000 messages behind is disconnected (close code 1013) and should do the same.
