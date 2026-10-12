from datetime import UTC, datetime

import pandas as pd

from openchart.market.aggregate import resample
from openchart.market.candle_builder import CandleBuilder, CandleUpdate
from openchart.market.timeframes import TIMEFRAMES
from openchart.providers.base import Candle, Tick

T = TIMEFRAMES
T0 = int(datetime(2024, 7, 10, 13, 0, tzinfo=UTC).timestamp())  # 09:00 NY: a 4h bar start


def tick(ts: float, bid: float, spread: float = 0.2) -> Tick:
    return Tick("XAU_USD", ts, bid, bid + spread)


class Harness:
    def __init__(self, stored: list[Candle] | None = None):
        self.updates: list[CandleUpdate] = []
        self.persisted: list[Candle] = []
        self.stored = stored or []

        async def load_m1(instrument, start, end):
            return [c for c in self.stored if start <= c.ts < end]

        self.builder = CandleBuilder(
            on_update=self.updates.append,
            on_m1_closed=lambda inst, c: self.persisted.append(c),
            load_m1=load_m1,
        )

    def warm_up(self) -> None:
        """A tick in the minute before T0, as on a server that has been running.

        The first minute after start is partial and never persisted; warming up makes
        T0 the first complete minute.
        """
        self.builder.on_tick(tick(T0 - 1, 1.0))

    def closed(self, tf: str) -> list[Candle]:
        return [u.candle for u in self.updates if u.timeframe == tf and u.closed]

    def last(self, tf: str) -> CandleUpdate:
        return [u for u in self.updates if u.timeframe == tf][-1]


async def test_sub_minute_bars_from_ticks():
    h = Harness()
    await h.builder.subscribe("XAU_USD", T["5s"], T0)
    for ts, bid in [(T0 + 0.5, 10), (T0 + 2, 12), (T0 + 4.9, 9), (T0 + 5.1, 11), (T0 + 7, 13)]:
        h.builder.on_tick(tick(ts, bid))
    [first] = h.closed("5s")
    assert (first.ts, first.bid, first.volume) == (T0, (10, 12, 9, 9), 3)
    live = h.last("5s")
    assert not live.closed and live.candle.ts == T0 + 5 and live.candle.bid == (11, 13, 11, 13)
    # The clock closes the bar once the period is over, even without another tick.
    h.builder.on_clock(T0 + 10 + 3.5)
    assert [c.ts for c in h.closed("5s")] == [T0, T0 + 5]


async def test_m1_closes_on_next_minute_tick_and_is_persisted():
    h = Harness()
    h.warm_up()
    h.builder.on_tick(tick(T0 + 1, 10))
    h.builder.on_tick(tick(T0 + 30, 11))
    assert h.persisted == []  # nothing closes mid-minute
    h.builder.on_tick(tick(T0 + 61, 12))
    [m1] = h.persisted
    assert (m1.ts, m1.bid, m1.volume, m1.complete) == (T0, (10, 11, 10, 11), 2, True)


async def test_clock_closes_m1_when_market_goes_quiet():
    h = Harness()
    h.warm_up()
    await h.builder.subscribe("XAU_USD", T["1m"], T0)
    h.builder.on_tick(tick(T0 + 10, 10))
    h.builder.on_clock(T0 + 61)  # within the grace period: still open
    assert h.persisted == []
    h.builder.on_clock(T0 + 64)
    assert [c.ts for c in h.persisted] == [T0]
    assert [c.ts for c in h.closed("1m")] == [T0]


async def test_late_ticks_are_dropped():
    h = Harness()
    h.builder.on_tick(tick(T0 + 61, 10))
    h.builder.on_tick(tick(T0 + 30, 99))  # belongs to a minute we never saw
    h.builder.on_tick(tick(T0 + 125, 11))  # closes T0+60
    h.builder.on_tick(tick(T0 + 100, 99))  # minute T0+60 already closed
    assert h.builder.late_ticks == 2
    assert all(99 not in c.bid for c in h.persisted)


async def test_4h_subscription_is_seeded_from_stored_m1():
    stored = [
        Candle(T0 + 60 * i, (5, 6, 4, 5), (5.2, 6.2, 4.2, 5.2), (5.1, 6.1, 4.1, 5.1), 10)
        for i in range(30)
    ]
    h = Harness(stored)
    now = T0 + 30 * 60 + 5
    await h.builder.subscribe("XAU_USD", T["4h"], now)
    h.builder.on_tick(tick(now, 8))
    live = h.last("4h")
    assert live.candle.ts == T0  # the 4h bar started before we subscribed
    assert live.candle.bid == (5, 8, 4, 8)
    assert live.candle.volume == 30 * 10 + 1


async def test_seed_uses_recent_bars_not_yet_written():
    h = Harness()  # nothing in the database yet
    for i in range(3):
        h.builder.on_tick(tick(T0 + 60 * i + 1, 10 + i))
    await h.builder.subscribe("XAU_USD", T["15m"], T0 + 125)
    h.builder.on_tick(tick(T0 + 126, 20))
    assert h.last("15m").candle.bid == (10, 20, 10, 20)
    assert h.last("15m").candle.volume == 4


async def test_live_bars_match_bars_derived_from_persisted_m1():
    """Live 5m bars equal resampled stored M1: the chart and history agree."""
    h = Harness()
    h.warm_up()
    await h.builder.subscribe("XAU_USD", T["5m"], T0)
    price = 100.0
    for i in range(1500):  # 25 minutes, one tick a second, with a few silent minutes
        ts = T0 + i + 0.5
        if 600 <= i < 720:
            continue
        price += (-1) ** (i // 7) * 0.13
        h.builder.on_tick(tick(ts, round(price, 2)))
    h.builder.on_clock(T0 + 1500 + 300 + 5)

    stored = pd.DataFrame(
        [(c.ts, *c.mid, c.volume) for c in h.persisted], columns=["ts", "o", "h", "l", "c", "v"]
    )
    derived = resample(stored, T["5m"])
    live = pd.DataFrame(
        [(c.ts, *c.mid, c.volume) for c in h.closed("5m")], columns=["ts", "o", "h", "l", "c", "v"]
    )
    pd.testing.assert_frame_equal(live, derived)
    assert len(live) == 5


async def test_unsubscribe_is_reference_counted():
    h = Harness()
    await h.builder.subscribe("XAU_USD", T["5s"], T0)
    await h.builder.subscribe("XAU_USD", T["5s"], T0)
    h.builder.unsubscribe("XAU_USD", T["5s"])
    assert h.builder.subscriptions("XAU_USD") == ["5s"]
    h.builder.unsubscribe("XAU_USD", T["5s"])
    assert h.builder.subscriptions("XAU_USD") == []


async def test_instruments_are_independent():
    h = Harness()
    await h.builder.subscribe("EUR_USD", T["1m"], T0)
    h.builder.on_tick(Tick("XAU_USD", T0 + 1, 2400, 2400.3))
    h.builder.on_tick(Tick("EUR_USD", T0 + 2, 1.08, 1.0801))
    assert {u.instrument for u in h.updates} == {"EUR_USD"}


async def test_first_minute_after_start_or_reconnect_is_not_persisted():
    h = Harness()
    h.builder.on_tick(tick(T0 + 40, 10))  # joined mid-minute
    h.builder.on_tick(tick(T0 + 61, 11))
    h.builder.on_tick(tick(T0 + 121, 12))
    assert [c.ts for c in h.persisted] == [T0 + 60]  # the partial T0 minute is skipped

    h.builder.mark_gap("XAU_USD")  # connection lost and restored
    h.builder.on_tick(tick(T0 + 300, 13))
    h.builder.on_tick(tick(T0 + 361, 14))
    h.builder.on_tick(tick(T0 + 421, 15))
    assert [c.ts for c in h.persisted] == [T0 + 60, T0 + 360]
