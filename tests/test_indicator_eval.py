"""The indicator alert evaluation (`alerts/indicator_eval.py`), with fake bars, a fake
computation and a fake clock, on a real database: closed bars only, arming, one
computation per group, replay equals live, catch-up after a restart, a sleep or a worker
outage, DST weeks and weekends, crossings exactly at zero, hostile results, a crash
between the event and the state, failure episodes, the broker's candles, the lifecycle."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import random

import numpy as np
import pandas as pd
import pytest

from openchart.alerts import indicator_eval as ie
from openchart.alerts.engine import CLOSE_GRACE_SECONDS
from openchart.alerts.indicator_eval import (
    IndicatorEvaluator,
    Resolved,
    advance,
    crossing,
    observation,
)
from openchart.indicators._custom import indicator_from_json
from openchart.indicators._registry import load_indicators
from openchart.market.sessions import bar_bounds
from openchart.market.timeframes import parse_timeframe
from openchart.store import alerts as alert_store
from openchart.store.alerts import Alert, AlertStore, IndicatorCondition, IndicatorState
from openchart.store.sqlite import Database

H = 3600
# Monday 2026-10-12 00:00 UTC: a trading week (FX hours: Sunday 17:00 to Friday 17:00 New York).
MONDAY = int(dt.datetime(2026, 10, 12, tzinfo=dt.UTC).timestamp())
NY = __import__("zoneinfo").ZoneInfo("America/New_York")

SCHEMA = indicator_from_json(
    "echo",
    {
        "name": "Echo",
        "title": "Echo",
        "overlay": True,
        "params": [
            {"name": "c", "type": "color", "default": "#2962ff"},
            {"name": "w", "type": "width", "default": 1, "min": 1, "max": 4},
        ],
        "outputs": [
            {"kind": "line", "name": "out", "label": "Out", "color": "c", "width": "w"},
            {"kind": "line", "name": "other", "label": "Other", "color": "c", "width": "w"},
            {
                "kind": "marker",
                "name": "buy",
                "label": "Buy",
                "color": "c",
                "shape": "arrow_up",
                "position": "below",
            },
            {"kind": "background", "name": "zone", "label": "Zone", "color": "c"},
        ],
    },
)


def trading(t: float) -> bool:
    """FX hours: Sunday 17:00 to Friday 17:00 in New York."""
    d = dt.datetime.fromtimestamp(t, NY)
    wd, mins = d.weekday(), d.hour * 60 + d.minute
    if wd == 5:
        return False
    if wd == 6:
        return mins >= 17 * 60
    if wd == 4:
        return mins < 17 * 60
    return True


class World:
    """Bars of one timeframe, a fake clock, and what the 'indicator' answers."""

    def __init__(self, tf="1h", start=MONDAY, count=60, closes=None, *, weekends=False):
        self.tf = parse_timeframe(tf)
        self.now = float(start)
        starts, t = [], start
        while len(starts) < count:
            s, e = bar_bounds(t, self.tf)
            if not weekends or trading(s) or trading(e - 1):
                starts.append(s)
            t = e
        self.starts = starts
        self.closes = list(closes) if closes is not None else [0.0] * count
        self.other = 0.0
        self.fail: Exception | None = None
        self.answer = None  # replace the result: a function of the frame
        self.computes = 0
        self.digest = "v1"
        self.warmup = 0
        self.checks = None
        self.delay = 0.0
        self.gate: asyncio.Event | None = None

    def end(self, i: int) -> int:
        return bar_bounds(self.starts[i], self.tf)[1]

    def at_close(self, i: int) -> float:
        """The moment bar `i` counts as closed."""
        return self.end(i) + CLOSE_GRACE_SECONDS

    # Bars
    async def read(self, instrument, tf, price, start, end, warm, now):
        rows = [(s, c) for s, c in zip(self.starts, self.closes, strict=False) if s < end]
        if start is None:
            rows = rows[-max(warm, 1) :]
        else:
            first = next((k for k, (s, _) in enumerate(rows) if s >= start), len(rows))
            rows = rows[max(first - warm, 0) :]
        frame = pd.DataFrame(
            {
                "ts": [s for s, _ in rows],
                "o": [c for _, c in rows],
                "h": [c for _, c in rows],
                "l": [c for _, c in rows],
                "c": [c for _, c in rows],
                "v": [1.0] * len(rows),
                "complete": [bar_bounds(s, self.tf)[1] <= now for s, _ in rows],
            }
        )
        return frame

    # Definitions
    async def resolve(self, kind, indicator_id):
        if kind == "builtin":
            ind = load_indicators().get(indicator_id)
            return Resolved(ind, "builtin", "ok")
        return Resolved(SCHEMA, self.digest, "ok")

    async def warmup_for(self, key, schema, settings):
        if key.kind == "builtin":
            return int(schema.warmup(settings))
        return self.warmup

    def checks_for(self, key, settings):
        return self.checks

    # The computation
    async def compute(self, key, schema, settings, frame):
        self.computes += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        if key.kind == "builtin":
            return schema.run(frame, settings)
        c = frame["c"].to_numpy(dtype=float)
        out = {
            "out": pd.Series(c),
            "other": pd.Series(np.full(len(c), self.other)),
            "buy": pd.Series((c > 0).astype(float)),
            "zone": pd.Series((c > 0).astype(float)),
        }
        if self.answer is not None:
            out = self.answer(frame, out)
        return out


class Defs:
    def __init__(self, world):
        self.world = world

    async def resolve(self, kind, indicator_id):
        return await self.world.resolve(kind, indicator_id)

    async def warmup(self, key, schema, settings):
        return await self.world.warmup_for(key, schema, settings)

    def checks(self, key, settings):
        return self.world.checks_for(key, settings)


@pytest.fixture
async def store(tmp_path):
    db = await Database.open(tmp_path / "t.db")
    yield AlertStore(db.conn)
    await db.close()


class Harness:
    def __init__(self, store, world, alerts=None):
        self.store, self.world = store, world
        self.alerts: dict[str, Alert] = alerts if alerts is not None else {}
        self.messages: list[str] = []
        self.fires: list[ie.Fire] = []
        self.wake = False
        self.ev = self.new()

    def new(self):
        return IndicatorEvaluator(
            store=self.store,
            alerts=lambda: list(self.alerts.values()),
            definitions=Defs(self.world),
            bars=self.world,
            compute=self.world.compute,
            now=lambda: self.world.now,
            wake=lambda: self.wake,
            traded=lambda _i, s, e: trading(s) or trading(e - 1),
        )

    async def add(
        self,
        condition="cross_value",
        value=0.0,
        *,
        output="out",
        other=None,
        kind="custom",
        ind="echo",
        params=None,
        frequency="repeating",
        direction="cross",
        tf=None,
        notify=("app",),
    ):
        cond = IndicatorCondition(
            uid="u1",
            kind=kind,
            id=ind,
            params=params or {},
            digest=(self.world.digest if kind == "custom" else "builtin"),
            output=output,
            condition=condition,
            other_output=other,
            value=value,
            name="Echo",
        )
        alert = Alert(
            id="",
            instrument="XAU_USD",
            type="indicator",
            timeframe=tf or self.world.tf.name,
            trigger_mode="candle_close",
            frequency=frequency,
            direction=direction,
            notify=list(notify),
            indicator=cond,
            istate=IndicatorState(),
        )
        alert = await self.store.create(alert)
        self.alerts[alert.id] = alert
        return alert

    async def step(self, now=None):
        if now is not None:
            self.world.now = now
        notice = await self.ev.step(self.world.now)
        self.messages += notice.messages
        self.fires += notice.fires
        return notice

    async def close(self, i):
        """Step at the moment bar `i` is closed."""
        return await self.step(self.world.at_close(i))

    async def restart(self):
        """A new evaluator on the alerts as stored (the server restarted)."""
        self.alerts = {a.id: a for a in await self.store.list()}
        self.ev = self.new()


def fired(fires):
    return [(f.bar, f.cross, f.value, f.other) for f in fires]


async def events(store):
    return sorted((e.bar, e.cross, e.value, e.late) for e in await store.events(1000))


# The rules ------------------------------------------------------------------------------


def test_the_crossing_rule_is_the_line_rule():
    assert crossing("cross_value", None, -1.0, 1.0) == "up"
    assert crossing("cross_value", None, 1.0, -1.0) == "down"
    assert crossing("cross_value", None, 0.0, 1.0) == "up"  # from exactly on it
    assert crossing("cross_value", None, -1.0, 0.0) is None  # onto it is not over it
    assert crossing("cross_value", None, 0.0, 0.0) is None
    assert crossing("cross_value", None, 0.0, -1.0) == "down"
    assert crossing("cross_value", None, None, 1.0) is None  # no previous observation
    assert crossing("cross_value", None, 1.0, None) is None
    assert crossing("signal", "marker", None, 1.0) == "signal"
    assert crossing("signal", "marker", 1.0, 1.0) == "signal"  # a marker on every bar
    assert crossing("signal", "background", 1.0, 1.0) is None  # still on: not turning on
    assert crossing("signal", "background", 0.0, 1.0) == "signal"
    assert crossing("signal", "background", None, 1.0) is None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (math.nan, 0.0),
        (0.0, math.nan),
        (math.inf, 0.0),
        (-math.inf, 0.0),
        (None, 1.0),
        (1e308, -1e308),
        ("x", 0.0),
    ],
)
def test_a_value_that_is_not_a_finite_number_is_no_observation(a, b):
    assert observation(a, b) is None


def test_the_max_warmup_is_the_routes():
    from openchart.api.routes_indicators import MAX_WARMUP

    assert ie.MAX_WARMUP == MAX_WARMUP


# Arming ----------------------------------------------------------------------------------


async def test_the_first_evaluation_records_and_never_fires(store):
    w = World(closes=[-1, -1, -1, 1, 1, -1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(3)  # bar 3 crossed up: the first evaluation only records it
    assert h.fires == [] and alert.istate.armed and alert.istate.last_bar == w.starts[3]
    assert alert.istate.prev == 1.0
    await h.close(5)
    assert fired(h.fires) == [(w.starts[5], "down", -1.0, 0.0)]
    stored = await store.get(alert.id)
    assert stored.istate.last_bar == w.starts[5] and stored.fire_count == 1


async def test_an_edit_or_a_rearm_records_again(store):
    w = World(closes=[-1, -1, 1, -1, 1, -1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    alert.istate = IndicatorState()  # what the API does on an edit (and `touched`)
    h.ev.touched(alert.id)
    await h.close(2)
    assert h.fires == []  # the bar that crossed is the first after the edit
    await h.close(3)
    assert len(h.fires) == 1


# Closed bars and timing --------------------------------------------------------------------


async def test_a_bar_is_evaluated_once_it_has_closed_and_only_once(store):
    w = World(closes=[-1, -1, 1, 1, 1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    await h.step(w.end(2) + CLOSE_GRACE_SECONDS - 0.001)  # not closed yet
    assert alert.istate.last_bar == w.starts[1] and h.fires == []
    await h.step(w.end(2) + CLOSE_GRACE_SECONDS)
    assert fired(h.fires) == [(w.starts[2], "up", 1.0, 0.0)]
    computes = w.computes
    for k in range(20):  # the loop runs every second: nothing new until bar 3 closes
        await h.step(w.at_close(2) + k)
    assert w.computes == computes and len(h.fires) == 1


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([-1, 0, 1], [("up", 2)]),  # onto zero, then over it: one cross, on the third bar
        ([1, 0, -1], [("down", 2)]),
        # Onto it and back: the line rule counts leaving it as a cross (prev 0 <= 0 < s).
        ([-1, 0, -1], [("down", 2)]),
        ([1, 0, 1], [("up", 2)]),
        ([0, 0, 0], []),
        ([-1, 0, 0, 1], [("up", 3)]),
    ],
)
async def test_a_crossing_exactly_on_the_value(store, closes, expected):
    lead = 5 if closes[0] > 0 else -5  # the arming bar, on the first value's side
    w = World(closes=[lead, *closes])
    h = Harness(store, w)
    await h.add(value=0.0)
    await h.close(0)
    for i in range(1, len(w.closes)):
        await h.close(i)
    assert [(f.cross, w.starts.index(f.bar) - 1) for f in h.fires] == expected


async def test_directions_and_frequencies(store):
    w = World(closes=[-1, -1, 1, -1, 1, -1, 1])
    h = Harness(store, w)
    up = await h.add(direction="up")
    once = await h.add(frequency="once")
    await h.close(0)
    for i in range(1, 7):
        await h.close(i)
    ups = [f for f in h.fires if f.alert is up]
    assert [f.cross for f in ups] == ["up", "up", "up"]
    assert [f.cross for f in h.fires if f.alert is once] == ["up"]
    assert once.status == "triggered" and (await store.get(once.id)).status == "triggered"


async def test_crossing_another_output_and_signals(store):
    w = World(closes=[-1, -1, 2, 2, -1, 3])
    w.other = 1.0
    h = Harness(store, w)
    cross = await h.add("cross_output", None, other="other")
    marker = await h.add("signal", None, output="buy")
    zone = await h.add("signal", None, output="zone")
    await h.close(0)
    for i in range(1, 6):
        await h.close(i)
    assert [(f.bar, f.cross) for f in h.fires if f.alert is cross] == [
        (w.starts[2], "up"),
        (w.starts[4], "down"),
        (w.starts[5], "up"),
    ]
    assert [f.bar for f in h.fires if f.alert is marker] == [w.starts[i] for i in (2, 3, 5)]
    assert [f.bar for f in h.fires if f.alert is zone] == [w.starts[i] for i in (2, 5)]


# One computation per group -----------------------------------------------------------------


async def test_alerts_on_the_same_instance_share_one_computation_per_bar(store):
    w = World(closes=[-1, 1, -1, 1])
    h = Harness(store, w)
    await h.add(value=0.0)
    await h.add(value=0.5, direction="up")
    await h.add("signal", None, output="buy")
    await h.add("cross_output", None, other="other")
    for i in range(4):
        await h.close(i)
    assert w.computes == 4  # one per closed bar, for four alerts
    await h.add(params={"w": 2})  # other values: another group
    await h.step(w.at_close(3) + 1)
    assert w.computes == 5


# Replay equals live -----------------------------------------------------------------------


def walk(n, seed=7):
    rng = random.Random(seed)
    x, out = 0.0, []
    for _ in range(n):
        x += rng.choice([-1.0, -0.5, 0.5, 1.0])
        out.append(x)
    return out


async def live_and_replay(tmp_path, world_args, add, *, reason_setup=None):
    """The same alerts on the same bars: evaluated at every close, and once at the end."""
    results = []
    for mode in ("live", "replay"):
        db = await Database.open(tmp_path / f"{mode}.db")
        try:
            w = World(**world_args)
            h = Harness(AlertStore(db.conn), w)
            alerts = [await add(h) for _ in range(1)]
            await h.close(0)
            if mode == "live":
                for i in range(1, len(w.starts)):
                    await h.close(i)
            else:
                if reason_setup:
                    reason_setup(h)
                await h.close(len(w.starts) - 1)
            results.append(
                (
                    fired(h.fires),
                    [(a.istate.last_bar, a.istate.prev, a.fire_count) for a in alerts],
                    [f.late for f in h.fires],
                )
            )
        finally:
            await db.close()
    return results


async def test_replay_over_many_bars_equals_live_closes(tmp_path):
    args = {"count": 80, "closes": walk(80)}
    live, replay = await live_and_replay(tmp_path, args, lambda h: h.add(value=0.25))
    assert live[0] and live[0] == replay[0] and live[1] == replay[1]  # bit for bit
    assert set(live[2]) == {None}  # live: on time
    assert set(replay[2][:-1]) == {"reconnect"} and replay[2][-1] in ("reconnect", None)


async def test_replay_equals_live_for_a_builtin_computed_here(tmp_path):
    args = {"count": 80, "closes": [2000 + x for x in walk(80, seed=3)]}

    async def add(h):
        return await h.add(
            value=2000.0,
            kind="builtin",
            ind="sma",
            output="sma",
            params={"length": 5, "source": "close"},
        )

    live, replay = await live_and_replay(tmp_path, args, add)
    assert live[0] and live[0] == replay[0] and live[1] == replay[1]


# Catch-up ---------------------------------------------------------------------------------


async def test_a_restart_replays_from_the_last_evaluated_bar(store):
    closes = walk(40, seed=11)
    w = World(count=40, closes=closes)
    h = Harness(store, w)
    alert = await h.add(value=0.25)
    for i in range(0, 15):
        await h.close(i)
    before = len(h.fires)
    await h.restart()  # stopped after bar 14; started again after bar 30 closed
    assert h.alerts[alert.id].istate.last_bar == w.starts[14]
    await h.close(30)
    replayed = h.fires[before:]
    # The same fires as live (computed separately), every one late "restart" but the newest.
    w2 = World(count=40, closes=closes)
    db_alert = h.alerts[alert.id]
    expected = []
    prev = None
    for i in range(14, 31):
        s = ie.observation(closes[i], 0.25)
        c = crossing("cross_value", None, prev, s) if i > 14 else None
        prev = s
        if c:
            expected.append((w2.starts[i], c))
    assert [(f.bar, f.cross) for f in replayed] == expected and expected
    assert {f.late for f in replayed if f.bar != w.starts[30]} == {"restart"}
    assert db_alert.istate.last_bar == w.starts[30]
    assert len(await store.events(1000)) == len(h.fires)


async def test_a_sleep_is_caught_up_and_marked_wake(store):
    w = World(count=30, closes=walk(30, seed=5))
    h = Harness(store, w)
    await h.add(value=0.25)
    for i in range(0, 5):
        await h.close(i)
    h.wake = True  # the feed noticed a freeze
    await h.close(25)
    late = [f.late for f in h.fires if w.starts[4] < f.bar < w.starts[25]]
    assert late and set(late) == {"wake"}


async def test_nothing_is_evaluated_until_prices_are_known_to_the_bars_end(store):
    w = World(closes=[-1, -1, 1, -1, 1])
    h = Harness(store, w)
    alert = await h.add()
    known = {"until": w.end(1)}
    h.ev.fresh = lambda _i, end: end <= known["until"]
    await h.close(1)
    computes = w.computes
    await h.close(2)
    await h.close(3)  # the feed is down: bars 2 and 3 wait, without a computation
    assert alert.istate.last_bar == w.starts[1] and h.fires == []
    assert w.computes == computes
    known["until"] = w.end(3)  # repaired and replayed by the line engine
    await h.step(w.at_close(3) + 5)
    assert [(f.bar, f.late) for f in h.fires] == [(w.starts[2], "reconnect"), (w.starts[3], None)]


async def test_more_than_the_catch_up_cap_is_skipped_with_one_message(store, monkeypatch):
    monkeypatch.setattr(ie, "CATCH_UP_MAX_SECONDS", 10 * H)
    w = World(count=40, closes=walk(40, seed=9))
    h = Harness(store, w)
    alert = await h.add(value=0.25)
    await h.close(0)
    await h.close(30)
    assert len([m for m in h.messages if "missed more than" in m]) == 1
    assert all(f.bar >= w.end(30) - 10 * H for f in h.fires)
    assert alert.istate.last_bar == w.starts[30]


# DST weeks and weekends --------------------------------------------------------------------


@pytest.mark.parametrize("tf", ["4h", "1D", "1W"])
async def test_session_bars_across_dst_and_the_weekend(store, tf):
    # From mid-October over the end of summer time in New York (Sunday 1 November 2026).
    start = int(dt.datetime(2026, 10, 18, 22, tzinfo=dt.UTC).timestamp())
    count = {"4h": 60, "1D": 20, "1W": 5}[tf]
    w = World(tf, start=start, count=count, closes=walk(count, seed=2), weekends=True)
    h = Harness(store, w)
    alert = await h.add(value=0.25)
    seen = []
    real = w.compute

    async def counting(key, schema, settings, frame):
        seen.append(int(frame["ts"].iloc[-1]))
        return await real(key, schema, settings, frame)

    h.ev.compute = counting
    t = w.at_close(0)
    end = w.at_close(count - 1)
    while t <= end:  # every minute of the period, weekends included
        await h.step(t)
        t += 60 if tf == "4h" else 300
    # Each bar was computed once, in order, at its own close; none was skipped.
    assert seen == w.starts[: len(seen)] and len(seen) == count
    assert alert.istate.last_bar == w.starts[-1]
    expected = []
    prev = None
    for i, c in enumerate(w.closes):
        s = observation(c, 0.25)
        x = crossing("cross_value", None, prev, s) if i else None
        prev = s
        if x:
            expected.append((w.starts[i], x))
    assert [(f.bar, f.cross) for f in h.fires] == expected
    assert all(f.late is None for f in h.fires)


async def test_friday_s_daily_bar_fires_at_friday_s_close_not_on_sunday(store):
    friday = int(dt.datetime(2026, 10, 14, 21, tzinfo=dt.UTC).timestamp())  # Wed 17:00 NY
    w = World("1D", start=friday, count=3, closes=[-1, 1, -1], weekends=True)
    h = Harness(store, w)
    await h.add(value=0.0)
    await h.close(0)
    await h.close(1)  # Friday 17:00 New York + 2 s
    assert [f.cross for f in h.fires] == ["up"]
    assert dt.datetime.fromtimestamp(h.fires[0].end, NY).weekday() == 4
    computes = w.computes
    for k in range(1, 48):  # the weekend: no new bar, no computation
        await h.step(w.at_close(1) + k * H)
    assert w.computes == computes


async def test_a_sleep_into_the_weekend_still_evaluates_friday_s_bar(store):
    tuesday = int(dt.datetime(2026, 10, 13, 21, tzinfo=dt.UTC).timestamp())  # Tue 17:00 NY
    w = World("1D", start=tuesday, count=4, closes=[-1, -1, 1, 1], weekends=True)
    h = Harness(store, w)
    await h.add(value=0.0)
    await h.close(1)  # Wednesday's bar; then the computer slept until Saturday evening
    saturday = int(dt.datetime(2026, 10, 17, 22, tzinfo=dt.UTC).timestamp())
    await h.step(saturday)
    assert [(f.bar, f.cross) for f in h.fires] == [(w.starts[2], "up")]  # Thu 17 to Fri 17


# Hostile results and failures ---------------------------------------------------------------


async def test_nan_and_infinity_are_no_observation(store):
    w = World(closes=[-1, -1, 1, -1, 1, 1, -1])
    h = Harness(store, w)
    alert = await h.add()

    def holes(frame, out):
        c = out["out"].to_numpy().copy()
        ts = frame["ts"].to_numpy()
        c[ts == w.starts[2]] = np.nan  # the bar that crossed up has no value
        c[ts == w.starts[5]] = np.inf
        return {**out, "out": pd.Series(c)}

    w.answer = holes
    for i in range(7):
        await h.close(i)
    # 2 is NaN (prev cleared), 3 records -1, 4 crosses up, 5 is inf (cleared), 6 records.
    assert [(w.starts.index(f.bar), f.cross) for f in h.fires] == [(4, "up")]
    assert alert.istate.prev == -1.0 and alert.istate.error is None


async def test_huge_values_still_cross(store):
    w = World(closes=[-1e300, -1e300, 1e300])
    h = Harness(store, w)
    await h.add()
    for i in range(3):
        await h.close(i)
    assert [f.cross for f in h.fires] == ["up"]


@pytest.mark.parametrize("bad", ["short", "long", "missing", "text"])
async def test_a_result_that_cannot_be_used_is_an_error_with_one_message(store, bad):
    w = World(count=12, closes=[-1, 1] * 6)
    h = Harness(store, w)
    alert = await h.add()
    await h.close(0)

    def broken(frame, out):
        c = out["out"]
        return {
            "short": {**out, "out": c.iloc[:-1]},
            "long": {**out, "out": pd.concat([c, c])},
            "missing": {k: v for k, v in out.items() if k != "out"},
            "text": {**out, "out": pd.Series(["x"] * len(c))},
        }[bad]

    w.answer = broken
    for i in range(1, 6):
        await h.close(i)
    assert alert.istate.error and alert.istate.last_bar == w.starts[0]
    assert len(h.messages) == 1 and h.fires == []
    assert (await store.get(alert.id)).istate.error == alert.istate.error
    w.answer = None
    await h.close(6)
    assert alert.istate.error is None and alert.istate.last_bar == w.starts[6]
    assert {f.late for f in h.fires if f.bar != w.starts[6]} <= {"reconnect"}
    assert len(h.fires) == 6  # bars 1 to 6 alternate: every one crossed


async def test_a_failing_worker_is_one_episode_one_message_and_a_late_catch_up(store):
    from openchart.indicators.worker.runner import ScriptFailed, WorkerUnavailable

    w = World(count=20, closes=[-1, 1] * 10)
    h = Harness(store, w)
    alert = await h.add()
    await h.close(0)
    w.fail = WorkerUnavailable("the indicator worker is restarting")
    for i in range(1, 6):  # five bars, and retries every second
        for k in range(0, 30):
            await h.step(w.at_close(i) + k)
    w.fail = ScriptFailed("timeout", {"type": "Timeout", "message": "timed out after 5 s"})
    await h.step(w.at_close(6))
    assert len([m for m in h.messages if "cannot be evaluated" in m]) == 1
    assert alert.istate.error and "timed out" in alert.istate.error
    assert alert.istate.last_bar == w.starts[0] and h.fires == []
    calls = w.computes
    await h.step(w.at_close(6) + 1)  # within RETRY_SECONDS: not tried again
    assert w.computes == calls
    w.fail = None
    await h.step(w.at_close(6) + ie.RETRY_SECONDS)
    assert len([m for m in h.messages if "evaluated again" in m]) == 1
    assert len(h.fires) == 6
    assert {f.late for f in h.fires if f.bar != w.starts[6]} == {"worker"}
    assert alert.istate.error is None and alert.istate.error_since is None
    assert len(h.messages) == 2


# The atomic write --------------------------------------------------------------------------


async def test_a_crash_between_the_event_and_the_state_leaves_neither(store, monkeypatch):
    w = World(closes=[-1, -1, 1, 1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    real = alert_store.atomic

    class Crashing:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql.startswith("UPDATE alerts"):
                raise RuntimeError("the process died here")
            return self.db.execute(sql, *args)

    async def crashing(conn, fn):
        return await real(conn, lambda db: fn(Crashing(db)))

    monkeypatch.setattr(alert_store, "atomic", crashing)
    with pytest.raises(RuntimeError):
        await h.close(2)
    monkeypatch.setattr(alert_store, "atomic", real)
    assert await store.events(10) == []  # the event went with the state
    assert (await store.get(alert.id)).istate.last_bar == w.starts[1]
    await h.restart()
    h.fires.clear()
    await h.close(3)
    assert [(f.bar, f.late) for f in h.fires] == [(w.starts[2], "restart")]
    assert [e.bar for e in await store.events(10)] == [w.starts[2]]


async def test_a_bar_already_in_the_log_is_not_logged_or_sent_twice(store):
    w = World(closes=[-1, -1, 1, 1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    # As if an older version had written the event but not the state.
    await store.record_indicator_step(
        [(ie._event(ie.Fire(alert, w.starts[2], w.end(2), "up", 1.0, 0.0, None), 0), alert)], []
    )
    await h.close(2)
    assert h.fires == []  # found again, already logged: nothing sent
    assert len(await store.events(10)) == 1
    assert (await store.get(alert.id)).istate.last_bar == w.starts[2]


# The broker's candles (D5) -----------------------------------------------------------------


async def test_a_corrected_bar_never_cancels_a_fire_but_is_recorded(store):
    w = World(closes=[-1, -1, 1, 1, 1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    await h.close(2)
    assert [f.cross for f in h.fires] == ["up"]
    w.closes[2] = -0.5  # the broker's candle for that bar: it did not cross after all
    await h.close(3)
    (event,) = await store.events(10)
    assert event.correction is not None
    assert event.correction["value"] == -0.5 and event.value == 1.0
    assert event.bar == w.starts[2]
    await h.close(4)  # recorded once
    assert (await store.events(10))[0].correction == event.correction
    assert len(h.fires) == 1 and alert.fire_count == 1


async def test_a_corrected_bar_that_still_fires_is_not_recorded(store):
    w = World(closes=[-1, -1, 1, 1])
    h = Harness(store, w)
    await h.add()
    await h.close(1)
    await h.close(2)
    w.closes[2] = 2.0
    await h.close(3)
    assert (await store.events(10))[0].correction is None


# Lookahead and warmup -----------------------------------------------------------------------


async def test_a_lookahead_script_is_still_evaluated_with_a_warning(store):
    w = World(closes=[-1, -1, 1])
    w.checks = {"lookahead": "warned", "warmup": "ok"}
    h = Harness(store, w)
    alert = await h.add()
    for i in range(3):
        await h.close(i)
    assert len(h.fires) == 1 and alert.istate.warnings == ["lookahead"]
    assert (await store.get(alert.id)).istate.warnings == ["lookahead"]


async def test_a_warmup_longer_than_the_bars_read_is_a_warning(store):
    w = World(closes=[-1, -1, 1])
    w.warmup = ie.MAX_WARMUP + 1
    w.checks = {"lookahead": "ok", "warmup": "ok"}
    h = Harness(store, w)
    alert = await h.add()
    await h.close(0)
    assert alert.istate.warnings == ["warmup_cut"]


# Lifecycle -------------------------------------------------------------------------------


async def test_a_new_file_version_rearms_and_a_missing_output_stops(store):
    w = World(closes=[-1, -1, 1, -1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(0)
    w.digest = "v2"
    await h.close(1)  # the file changed: re-armed on v2, evaluated from the next run
    assert alert.indicator.digest == "v2" and not alert.istate.armed
    await h.close(2)
    assert h.fires == []  # the first evaluation after the edit records
    await h.close(3)
    assert [f.cross for f in h.fires] == ["down"]


async def test_instances_changed_rearms_stops_and_says_why_once(store):
    w = World(closes=[-1, -1, 1])
    h = Harness(store, w)
    a = await h.add()
    b = await h.add(value=0.5)
    c = await h.add(value=0.7)
    await h.close(0)
    schema = Resolved(SCHEMA, "v1", "ok")
    # Same values: nothing. Other values: re-armed.
    notice = await h.ev.instances_changed("XAU_USD", {"u1": (schema, {})})
    assert notice.changed == [] and a.istate.armed
    notice = await h.ev.instances_changed("XAU_USD", {"u1": (schema, {"length": 3})})
    assert {x.id for x in notice.changed} == {a.id, b.id, c.id}
    assert not a.istate.armed and a.indicator.params == {"length": 3}
    # Removed from the chart: stopped, settings kept, one message for all three.
    notice = await h.ev.instances_changed("XAU_USD", {})
    assert [x.status for x in (a, b, c)] == ["stopped"] * 3
    assert a.stop_reason == ie.STOP_REMOVED and len(notice.messages) == 1
    stored = await store.get(a.id)
    assert stored.status == "stopped" and stored.stop_reason == ie.STOP_REMOVED
    assert stored.indicator.value == 0.0 and stored.indicator.output == "out"


async def test_an_output_removed_by_an_edit_stops_the_alert(store):
    w = World(closes=[-1, -1, 1])
    h = Harness(store, w)
    a = await h.add()
    await h.close(0)
    smaller = indicator_from_json(
        "echo",
        {
            "name": "Echo",
            "title": "Echo",
            "overlay": True,
            "params": [
                {"name": "c", "type": "color", "default": "#2962ff"},
                {"name": "w", "type": "width", "default": 1, "min": 1, "max": 4},
            ],
            "outputs": [
                {"kind": "line", "name": "renamed", "label": "R", "color": "c", "width": "w"}
            ],
        },
    )
    notice = await h.ev.instances_changed("XAU_USD", {"u1": (Resolved(smaller, "v2", "ok"), {})})
    assert a.status == "stopped" and "no output 'out'" in a.stop_reason
    assert len(notice.messages) == 1


async def test_an_edit_during_a_run_wins(store):
    w = World(closes=[-1, -1, 1])
    h = Harness(store, w)
    alert = await h.add()
    await h.close(1)
    w.gate = asyncio.Event()
    before = w.computes
    run = asyncio.ensure_future(h.close(2))
    for _ in range(10_000):
        if w.computes > before:
            break
        await asyncio.sleep(0)
    assert w.computes == before + 1, "the run did not reach the computation"
    alert.istate = IndicatorState()  # the API re-armed it meanwhile
    h.ev.touched(alert.id)
    w.gate.set()
    await run
    assert h.fires == [] and not alert.istate.armed
    assert (await store.get(alert.id)).istate.last_bar == w.starts[1]


# The watchdog -------------------------------------------------------------------------------


async def test_behind_is_one_message_per_episode(store):
    w = World(count=20, closes=[-1, 1] * 10)
    h = Harness(store, w)
    alert = await h.add()
    await h.close(0)
    h.ev.fresh = lambda _i, end: end <= w.end(0)  # no prices after bar 0
    msgs = []
    for i in range(1, 8):
        await h.close(i)
        msgs += h.ev.behind(w.at_close(i))
    assert len(msgs) == 1 and "behind" in msgs[0] and alert.istate.behind
    h.ev.fresh = lambda _i, _end: True
    await h.close(8)
    msgs += h.ev.behind(w.at_close(8))
    assert len(msgs) == 2 and "caught up" in msgs[1] and not alert.istate.behind


def test_advance_ignores_bars_already_evaluated():
    cond = IndicatorCondition("u", "custom", "echo", {}, "v1", "out", "cross_value", value=0.0)
    alert = Alert(
        id="a",
        instrument="X",
        type="indicator",
        indicator=cond,
        istate=IndicatorState(last_bar=200, prev=-1.0, armed=True),
        frequency="repeating",
    )
    bars = [(100, 200), (200, 300), (300, 400)]
    got = advance(
        alert,
        bars,
        [(1.0, 0.0), (1.0, 0.0), (1.0, 0.0)],
        kind=None,
        newest=300,
        now=402,
        reason="restart",
    )
    assert [(f.bar, f.cross, f.late) for f in got] == [(300, "up", None)]
    assert alert.istate.last_bar == 300


def test_values_unused_json():
    assert json.dumps(IndicatorState().to_json())
