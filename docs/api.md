# API

All REST routes are under `/api` and return JSON. Times are UTC epoch seconds. Prices are floats.

> Until M4 there is no login: the server binds to `127.0.0.1` by default. From M4 every route except `/api/health`, `/api/setup/status`, `/api/setup` and `/api/login` needs a session cookie.

## Timeframes

`1s 5s 15s 30s 1m 5m 15m 30m 1h 4h 1D 1W`

- Sub-minute bars come from stored ticks (tick storage must be on). REST serves at most the last 3 days of them.
- `1m` to `1h` align to multiples of their length.
- `4h`, `1D` and `1W` are anchored to the trading-day close, 17:00 New York: 4h bars open at 17, 21, 01, 05, 09 and 13 New York time, a daily bar runs 17:00 to 17:00, and a weekly bar starts Friday 17:00. At a DST change these bars are an hour shorter or longer, as with the broker's own candles.
- A bar's `t` is its **start**. So Monday's daily bar has `t` = Sunday 17:00 New York, as in OANDA's own data. Clients label daily bars by trading date.

## REST

| Route | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` |
| `GET /api/version` | `{"version", "api_version"}` |
| `GET /api/status` | feed state: `provider`, `provider_state` (`connecting`, `connected`, `reconnecting`, `stopped`, `not_configured`, `no_instruments`), `reconnects`, `error`, and per instrument `last_tick_age` (seconds) and `history` (backfill state) |
| `GET /api/instruments` | stored instruments with `decimals`, `pip_size`, `display_name`, trading `hours` |
| `GET /api/candles` | see below |

### `GET /api/candles`

| Parameter | Default | |
|---|---|---|
| `instrument` | required | e.g. `XAU_USD` |
| `timeframe` | `1m` | |
| `price` | `mid` | `mid`, `bid` or `ask` |
| `from` | none | first bar start to include. A bar that starts before `from` is left out, not cut in half |
| `to` | now | exclusive |
| `limit` | 1000 (max 5000) | with `from`: the first `limit` bars from there; without `from`: the latest `limit` bars |

```json
{"instrument": "XAU_USD", "timeframe": "1h", "price": "mid", "decimals": 3,
 "candles": [{"t": 1791838800, "o": 4174.4, "h": 4175.885, "l": 4166.705, "c": 4173.68,
              "v": 8104, "complete": false}]}
```

`v` is **tick volume** (number of price updates), not traded volume. Only the last bar can have `"complete": false` (its period is not over yet).

Errors: `400` unknown timeframe or price, `404` unknown instrument, `422` invalid parameter.

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
| `error` `{error}` | invalid message |

A tick is always sent before the candle update it causes.

A live bar for `1m` and above starts from stored history: subscribing to `4h` mid-period gives the whole 4h bar so far, not just the ticks since subscribing. It uses the same arithmetic as `/api/candles`, so live and historical bars agree.

**Reconnecting:** after a dropped connection, fetch `/api/candles?from=<t of your last bar>` and resubscribe; the bar at `from` is included and replaces yours. A client that falls 2000 messages behind is disconnected (close code 1013) and should do the same.
