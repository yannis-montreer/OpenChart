"""Alert engine (SPEC section 9): cross detection, trigger options, re-arming, catch-up."""

from datetime import UTC, datetime

import pytest

from openchart.alerts.engine import AlertEngine
from openchart.market.instruments import METAL_HOURS
from openchart.market.sessions import TradingClock
from openchart.providers.base import OHLC, Candle, Tick
from openchart.store.alerts import Alert
from openchart.store.drawings import Drawing


def utc(*a) -> float:
    return datetime(*a, tzinfo=UTC).timestamp()


T0 = utc(2024, 7, 10, 12)  # Wednesday, gold open
GOLD = TradingClock(METAL_HOURS)


def engine(*alerts: Alert, drawings=(), clock=GOLD, gaps=False) -> AlertEngine:
    # Most tests jump in time between ticks; gap detection is tested on its own.
    e = AlertEngine(now=lambda: T0, gap_seconds=120 if gaps else 1e12)
    e.load(alerts, drawings)
    e.set_clock("XAU_USD", clock)
    return e


def alert(**kw) -> Alert:
    base = {"id": "a1", "instrument": "XAU_USD", "type": "level_cross", "level": 100.0}
    return Alert(**{**base, **kw})


def line(kind: str, *points: tuple[float, float], id: str = "d1") -> Drawing:
    return Drawing(id, "XAU_USD", kind, [{"t": t, "p": p} for t, p in points])


def tick(e: AlertEngine, t: float, price: float, spread: float = 0.0) -> None:
    e.on_tick(Tick("XAU_USD", t, price - spread / 2, price + spread / 2))


def fires(e: AlertEngine) -> list:
    out = []
    while not e.fires.empty():
        out.append(e.fires.get_nowait())
    return out


def walk(e: AlertEngine, prices, start: float = T0, step: float = 1.0) -> list:
    for i, p in enumerate(prices):
        tick(e, start + i * step, p)
    return fires(e)


# Level and direction


def test_level_cross_up_and_down():
    e = engine(alert(frequency="repeating"))
    got = walk(e, [99, 99.5, 100.5, 101, 99.9])
    assert [(f.cross, f.price) for f in got] == [("up", 100.5), ("down", 99.9)]
    assert got[0].line_value == 100.0 and got[0].late is None


@pytest.mark.parametrize(("direction", "expected"), [("up", ["up"]), ("down", ["down"])])
def test_direction_is_respected(direction, expected):
    e = engine(alert(direction=direction, frequency="repeating"))
    assert [f.cross for f in walk(e, [99, 101, 99])] == expected


def test_the_first_observation_never_fires():
    e = engine(alert())
    assert walk(e, [105]) == []  # already above: nothing crossed


def test_touching_the_line_then_leaving_counts_once():
    e = engine(alert(frequency="repeating"))
    # s_prev <= 0 < s: sitting exactly on the line and then going up is the cross.
    assert [f.cross for f in walk(e, [99, 100, 100, 101])] == ["up"]


@pytest.mark.parametrize(("which", "fired"), [("bid", False), ("ask", True), ("mid", True)])
def test_trigger_price(which, fired):
    e = engine(alert(trigger_price=which))
    for i, mid in enumerate([99.0, 100.05]):  # spread 0.2: bid 99.95, ask 100.15
        tick(e, T0 + i, mid, spread=0.2)
    assert bool(fires(e)) is fired


# Line geometry: sloped, segment, ray, gap-through


def test_sloped_trendline_fires_at_the_line_not_at_the_anchors():
    # Falls 1.0 per hour of trading time: 100 at T0, 98 two hours later.
    trend = line("trendline", (T0, 100.0), (T0 + 7200, 98.0))
    e = engine(
        alert(type="line_cross", drawing_id="d1", level=None, frequency="repeating"),
        drawings=[trend],
    )
    t = T0 + 3600  # the line is at 99
    tick(e, t, 98.9)
    tick(e, t + 1, 99.05)  # above 99 (but far below the anchor at 100)
    [f] = fires(e)
    assert f.cross == "up" and f.line_value == pytest.approx(99.0, abs=1e-3)


def test_a_segment_only_exists_between_its_points():
    trend = line("trendline", (T0, 100.0), (T0 + 600, 100.0))
    e = engine(
        alert(type="line_cross", drawing_id="d1", level=None, frequency="repeating"),
        drawings=[trend],
    )
    walk(e, [99, 101], start=T0 - 100)  # before the segment: nothing to cross
    assert walk(e, [99, 101], start=T0 + 700) == []  # after it
    # Entering the segment needs one observation before a cross counts.
    assert [f.cross for f in walk(e, [101, 99], start=T0 + 10)] == ["down"]


def test_a_ray_starts_at_its_first_point():
    ray = line("ray", (T0, 100.0), (T0 + 60, 100.0))
    e = engine(
        alert(type="line_cross", drawing_id="d1", level=None, frequency="repeating"), drawings=[ray]
    )
    assert walk(e, [99, 101], start=T0 - 10) == []
    assert [f.cross for f in walk(e, [101, 99], start=T0 + 10_000)] == ["down"]  # far beyond


def test_a_jump_over_the_line_between_two_ticks_is_a_cross():
    e = engine(alert())
    [f] = walk(e, [95, 108])  # gap through the level
    assert f.cross == "up" and f.price == 108


def test_extended_line_holds_before_its_points():
    ext = line("extended", (T0, 100.0), (T0 + 3600, 101.0))
    e = engine(alert(type="line_cross", drawing_id="d1", level=None), drawings=[ext])
    assert [f.cross for f in walk(e, [98, 99.5], start=T0 - 3600)] == ["up"]  # line at 99 there


# Re-arming


def test_moving_the_line_over_the_price_is_not_a_cross():
    e = engine(
        alert(type="line_cross", drawing_id="d1", level=None, frequency="repeating"),
        drawings=[line("hline", (T0, 100.0))],
    )
    walk(e, [99, 99.2])  # below the line
    e.drawing_changed(line("hline", (T0, 98.0)))  # dragged below the price
    assert walk(e, [99.3, 99.4], start=T0 + 10) == []  # fresh observation first
    assert [f.cross for f in walk(e, [97.5], start=T0 + 20)] == ["down"]  # a real cross


def test_editing_the_alert_rearms_it():
    a = alert(frequency="repeating")
    e = engine(a)
    walk(e, [99])
    e.put(a)  # edited
    assert walk(e, [101], start=T0 + 5) == []


def test_restyling_the_line_keeps_the_alert_armed():
    d = line("hline", (T0, 100.0))
    e = engine(alert(type="line_cross", drawing_id="d1", level=None), drawings=[d])
    walk(e, [99])
    e.drawing_changed(Drawing("d1", "XAU_USD", "hline", d.points, {"color": "#ff0000"}))
    assert len(walk(e, [101], start=T0 + 5)) == 1


# Frequency, cooldown, expiry


def test_once_fires_once_then_is_triggered():
    a = alert(frequency="once")
    e = engine(a)
    assert len(walk(e, [99, 101, 99, 101])) == 1
    assert a.status == "triggered" and a.fire_count == 1


def test_once_per_bar():
    a = alert(frequency="once_per_bar", timeframe="1h")
    e = engine(a)
    assert len(walk(e, [99, 101, 99, 101], step=60)) == 1  # same hour
    assert len(walk(e, [99, 101], start=T0 + 3600, step=60)) == 1  # next hour


def test_cooldown():
    e = engine(alert(frequency="repeating", cooldown_s=60))
    assert len(walk(e, [99, 101, 99, 101], step=10)) == 1
    assert len(walk(e, [99, 101], start=T0 + 100, step=1)) == 1


def test_expired_alerts_stop():
    a = alert(frequency="repeating", expires_at=int(T0 + 5))
    e = engine(a)
    assert walk(e, [99, 101], start=T0 + 10) == []
    e.on_clock(T0 + 10)
    assert a.status == "expired" and e.changed == [a]


# Candle close


def test_candle_close_ignores_crossing_inside_the_bar():
    a = alert(trigger_mode="candle_close", timeframe="1h", frequency="repeating")
    e = engine(a)
    bar = utc(2024, 7, 10, 12)
    walk(e, [99, 99.5], start=bar - 120, step=60)  # previous bar closes below
    walk(e, [99, 101, 102, 99.5], start=bar + 60, step=600)  # above, but closes below
    assert walk(e, [99.6], start=bar + 3601) == []  # the 12:00 bar closed at 99.5


def test_candle_close_fires_at_the_close_with_the_line_at_close_time():
    trend = line("extended", (T0, 100.0), (T0 + 3600, 101.0))  # +1 per open hour
    a = alert(
        type="line_cross", drawing_id="d1", level=None, trigger_mode="candle_close", timeframe="1h"
    )
    e = engine(a, drawings=[trend])
    walk(e, [99, 99.5], start=T0 - 120, step=60)  # 11:00 bar closes below 100
    walk(e, [100.2, 101.3], start=T0 + 60, step=1200)  # 12:00 bar ends at 101.3
    e.on_clock(T0 + 3600 + 5)  # no tick after the bar: the clock closes it
    [f] = fires(e)
    assert f.ts == T0 + 3600 and f.price == 101.3
    assert f.line_value == pytest.approx(101.0)  # the line at 13:00, not at the last tick


# Trading time: the line the alert checks is the line on the chart


def test_a_sloped_line_keeps_its_trading_time_slope_over_the_weekend():
    thu, fri = utc(2024, 7, 11, 13), utc(2024, 7, 12, 13)  # -1 per trading day
    ray = line("ray", (thu, 100.0), (fri, 99.0))
    monday = utc(2024, 7, 15, 13)  # the chart shows 98 here (clock time would say 96)
    e = engine(alert(type="line_cross", drawing_id="d1", level=None), drawings=[ray])
    tick(e, monday, 97.5)
    tick(e, monday + 1, 97.9)
    assert fires(e) == []  # still below 98
    tick(e, monday + 2, 98.1)
    [f] = fires(e)
    assert f.line_value == pytest.approx(98.0, abs=1e-4)


def test_learned_closures_are_used():
    labor_day = (int(utc(2024, 9, 2, 17, 30)), int(utc(2024, 9, 2, 22)))
    a, b = utc(2024, 9, 2, 13, 30), utc(2024, 9, 2, 16, 30)  # -1 per open hour
    ray = line("ray", (a, 100.0), (b, 97.0))
    e = engine(
        alert(type="line_cross", drawing_id="d1", level=None),
        drawings=[ray],
        clock=TradingClock(METAL_HOURS, [labor_day]),
    )
    assert e.line_value(e.alerts["a1"], labor_day[1]) == pytest.approx(96.0)


# Catch-up after a gap (sleep, outage, restart)


def candle(ts: float, o: float, h: float, low: float, c: float) -> Candle:
    m = OHLC(o, h, low, c)
    return Candle(int(ts), m, m, m, 1)


def test_a_gap_waits_for_catch_up_and_replays_candles_as_late():
    a = alert(frequency="repeating")
    e = engine(a, gaps=True)
    walk(e, [99, 99.5])  # last observation at T0 + 1
    e.next_reason = "wake"  # the feed noticed the freeze
    wake = T0 + 2 * 3600 + 15
    tick(e, wake, 99.2)  # back below: without the candles the cross would be missed
    tick(e, wake + 1, 99.3)
    assert fires(e) == [] and "XAU_USD" in e.gaps
    start, end = e.catch_up_range("XAU_USD")
    assert (start, end) == (int(T0) + 60, int(wake) // 60 * 60)
    candles = [candle(start + 60 * i, 99.5, 99.6, 99.4, 99.5) for i in range(30)]
    candles.append(candle(start + 1800, 99.5, 100.8, 99.4, 100.5))  # crossed up
    candles.append(candle(start + 1860, 100.5, 100.6, 99.1, 99.2))  # and down again
    assert e.catch_up("XAU_USD", candles) == 2
    got = fires(e)
    assert [(f.cross, f.late) for f in got] == [("up", "wake"), ("down", "wake")]
    assert got[0].ts == start + 1800 + 40  # the high of that minute
    assert "XAU_USD" not in e.gaps
    assert [f.late for f in walk(e, [101], start=wake + 5)] == [None]  # live again


def test_no_gap_over_a_weekend_or_a_short_pause():
    e = engine(alert(), gaps=True)
    friday, sunday = utc(2024, 7, 12, 20, 59, 50), utc(2024, 7, 14, 22, 0, 5)  # close, reopen
    walk(e, [99], start=friday)
    tick(e, sunday, 99.1)
    tick(e, sunday + 90, 99.2)  # 90 s pause
    assert e.gaps == {}


def test_after_a_restart_the_first_tick_opens_a_restart_gap():
    e = engine(alert(), gaps=True)
    e.last_eval["XAU_USD"] = T0  # loaded from the database
    tick(e, T0 + 3600, 99)
    assert e.gaps["XAU_USD"].reason == "restart"
    e.catch_up("XAU_USD", [])
    tick(e, T0 + 3601, 99.1)
    assert e.next_reason == "reconnect"
