import pytest

from openchart.providers.base import AuthError, Heartbeat, Tick
from openchart.providers.fake import STALL, FakeProvider
from openchart.providers.stream import ResilientTickStream, StreamState


def tick(n: int) -> Tick:
    return Tick("XAU_USD", 1_700_000_000 + n, 2000 + n, 2000.3 + n)


class Recorder:
    def __init__(self):
        self.sleeps: list[float] = []
        self.connected: list[bool] = []
        self.states: list[StreamState] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def on_connected(self, reconnect: bool) -> None:
        self.connected.append(reconnect)

    def on_state(self, state: StreamState, detail: str) -> None:
        self.states.append(state)


def make_stream(provider, rec: Recorder, **kw) -> ResilientTickStream:
    return ResilientTickStream(
        provider,
        ["XAU_USD"],
        sleep=rec.sleep,
        on_connected=rec.on_connected,
        on_state=rec.on_state,
        **kw,
    )


async def take(stream: ResilientTickStream, n: int) -> list[Tick]:
    out = []
    gen = stream.ticks()
    async for t in gen:
        out.append(t)
        if len(out) == n:
            break
    await gen.aclose()
    return out


async def test_reconnects_after_network_error():
    provider = FakeProvider(
        sessions=[
            [tick(1), ConnectionResetError("cable pulled")],
            [Heartbeat(0), tick(2)],
        ]
    )
    rec = Recorder()
    stream = make_stream(provider, rec)
    assert await take(stream, 2) == [tick(1), tick(2)]
    assert rec.connected == [False, True]  # second connect triggers gap-fill
    assert stream.reconnects == 1
    assert len(rec.sleeps) == 1


async def test_missing_heartbeat_counts_as_dead_link():
    provider = FakeProvider(sessions=[[Heartbeat(0), STALL], [tick(5)]])
    rec = Recorder()
    stream = make_stream(provider, rec, heartbeat_timeout=0.05)
    assert await take(stream, 1) == [tick(5)]
    assert provider.stream_calls == 2
    assert StreamState.RECONNECTING in rec.states


async def test_backoff_grows_and_resets_after_success():
    err = OSError("down")
    provider = FakeProvider(
        sessions=[[err], [err], [err], [tick(1), err], [err], [tick(2)]],
    )
    rec = Recorder()
    stream = make_stream(provider, rec, backoff_initial=1, backoff_max=60)
    await take(stream, 2)
    # Jitter keeps each delay within [50%, 100%] of 1, 2, 4, then reset to 1 after a tick.
    upper = [1, 2, 4, 1, 2]
    assert len(rec.sleeps) == len(upper)
    for got, cap in zip(rec.sleeps, upper, strict=True):
        assert cap / 2 <= got <= cap


async def test_backoff_is_capped():
    stream = ResilientTickStream(FakeProvider(), [], backoff_initial=1, backoff_max=60)
    assert all(stream.backoff_delay(n) <= 60 for n in range(20))


async def test_auth_error_stops_the_stream():
    provider = FakeProvider(sessions=[[AuthError("token revoked")]])
    rec = Recorder()
    with pytest.raises(AuthError):
        await take(make_stream(provider, rec), 1)
    assert rec.states[-1] == StreamState.STOPPED


async def test_failing_gap_fill_does_not_kill_the_feed():
    provider = FakeProvider(sessions=[[tick(1), OSError()], [tick(2)]])
    rec = Recorder()

    async def broken(reconnect: bool) -> None:
        raise RuntimeError("gap-fill failed")

    stream = make_stream(provider, rec)
    stream.on_connected = broken
    assert await take(stream, 2) == [tick(1), tick(2)]
