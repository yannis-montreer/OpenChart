"""In-process stand-in for OANDA's candles endpoint, for use with httpx.MockTransport."""

import httpx

TOKEN = "test-token-abcdef-9876"


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


class CandleServer:
    """Mimics OANDA's `from` + `count` pagination over a fixed candle list.

    Like the real API, `includeFirst=false` drops the candle at `from` but it still
    counts toward `count`, so such a page holds `count - 1` candles.
    """

    def __init__(self, stamps: list[int], incomplete_last: bool = False):
        self.stamps = stamps
        self.incomplete_last = incomplete_last
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["Accept-Datetime-Format"] == "UNIX"
        q = request.url.params
        start, count = int(q["from"]), int(q["count"])
        sel = [t for t in self.stamps if t >= start][:count]
        if q.get("includeFirst", "true") == "false" and sel and sel[0] == start:
            sel = sel[1:]
        last = self.stamps[-1]
        body = [raw_candle(t, not (self.incomplete_last and t == last)) for t in sel]
        return httpx.Response(200, json={"instrument": "XAU_USD", "candles": body})
