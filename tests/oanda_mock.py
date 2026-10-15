"""In-process stand-in for OANDA's candles endpoint, for use with httpx.MockTransport.

It enforces OANDA's rules for the parameters our client sends, so a request the real API
would refuse fails in the tests too (the error texts are paraphrased):

- `count` from 1 to 5000;
- `from`, `to` and `count` never all three together;
- times as UNIX seconds (the client asks for `Accept-Datetime-Format: UNIX`), not in the
  future;
- `price` made of M, B and A; a known granularity;
- an `Authorization: Bearer <token>` header. A token that is not printable text gets an
  empty 400 like the one in front of the real API (owner's check, 2026-10-15); a wrong
  but readable token gets 401.
"""

import time

import httpx

TOKEN = "test-token-abcdef-9876"
MAX_COUNT = 5000
GRANULARITIES = {"S5", "M1", "M5", "M15", "M30", "H1", "H4", "D", "W"}


def raw_candle(ts: int, complete: bool = True) -> dict:
    px = {"o": "2000.0", "h": "2001.0", "l": "1999.0", "c": "2000.5"}
    return {
        "time": f"{ts}.000000000",
        "bid": px,
        "ask": px,
        "mid": px,
        "volume": 7,
        "complete": complete,
    }


def oanda_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessage": message})


class CandleServer:
    """Mimics OANDA's `from` + `count` and `to` + `count` pagination over a fixed list.

    Like the real API, `includeFirst=false` drops the candle at `from` but it still
    counts toward `count`, so such a page holds `count - 1` candles. Going backward,
    the candle at `to` is returned and counted too (the worst case for the client),
    and without `from` or `to` the page ends with the latest, possibly forming candle.
    """

    def __init__(self, stamps: list[int], incomplete_last: bool = False, now: float | None = None):
        self.stamps = stamps
        self.incomplete_last = incomplete_last
        self.now = now  # the server's clock for "in the future" (default: the real one)
        self.requests: list[httpx.Request] = []
        self.refused: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        refusal = self.check(request)
        if refusal is not None:
            self.refused.append(refusal.text or "(empty)")
            return refusal
        q = request.url.params
        count = int(q["count"])
        if "from" in q:
            start = int(float(q["from"]))
            sel = [t for t in self.stamps if t >= start][:count]
            if q.get("includeFirst", "true") == "false" and sel and sel[0] == start:
                sel = sel[1:]
        elif "to" in q:
            sel = [t for t in self.stamps if t <= int(float(q["to"]))][-count:]
        else:
            sel = self.stamps[-count:]
        last = self.stamps[-1] if self.stamps else None
        body = [raw_candle(t, not (self.incomplete_last and t == last)) for t in sel]
        return httpx.Response(200, json={"instrument": "XAU_USD", "candles": body})

    def check(self, request: httpx.Request) -> httpx.Response | None:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or not token.isprintable() or " " in token:
            return httpx.Response(400, content=b"")  # refused in front of the API
        if token != TOKEN:
            return oanda_error(401, "Insufficient authorization to perform request.")
        if request.headers.get("Accept-Datetime-Format") != "UNIX":
            return oanda_error(400, "Invalid value specified for 'Accept-Datetime-Format'")
        q = request.url.params
        now = time.time() if self.now is None else self.now
        if q.get("granularity", "S5") not in GRANULARITIES:
            return oanda_error(400, "Invalid value specified for 'granularity'")
        price = q.get("price", "M")
        if not price or any(ch not in "MBA" for ch in price):
            return oanda_error(400, "Invalid value specified for 'price'")
        if "count" in q:
            try:
                count = int(q["count"])
            except ValueError:
                return oanda_error(400, "Invalid value specified for 'count'")
            if count > MAX_COUNT:
                return oanda_error(400, f"Maximum value for 'count' exceeded ({MAX_COUNT})")
            if count < 1:
                return oanda_error(400, "Invalid value specified for 'count'")
        if "from" in q and "to" in q and "count" in q:
            return oanda_error(400, "'count' cannot be used together with both 'from' and 'to'")
        for name in ("from", "to"):
            if name in q:
                try:
                    value = float(q[name])
                except ValueError:
                    return oanda_error(400, f"Invalid value specified for '{name}'")
                if value > now:
                    return oanda_error(400, f"Invalid value specified for '{name}': in the future")
        if q.get("includeFirst", "true") not in ("true", "false"):
            return oanda_error(400, "Invalid value specified for 'includeFirst'")
        return None
