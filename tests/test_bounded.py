import anyio
import pytest

from tests.bounded import call


class Portal:
    def call(self, fn):
        return anyio.run(fn)


class Client:
    portal = Portal()


async def forever():
    await anyio.sleep_forever()


async def answer(x):
    return x + 1


def test_a_call_that_never_finishes_fails_with_its_name():
    with pytest.raises(AssertionError, match="forever did not finish within 0s"):
        call(Client(), forever, timeout=0.05)


def test_a_call_returns_its_result():
    assert call(Client(), answer, 1) == 2
    assert call(Client(), len, "abc") == 3


async def test_drain_returns_when_a_finished_task_is_still_listed():
    """The hang of PR #53's Windows 3.13 job: `drain` spun on a done `_system` task that was
    still in `tasks`, and the test never ended."""
    import asyncio

    from openchart.alerts.notify import Dispatcher

    dispatcher = Dispatcher(store=None, notifier=None)  # type: ignore[arg-type]
    done = asyncio.ensure_future(asyncio.sleep(0))
    await done
    dispatcher.tasks.add(done)  # as if its discard callback had not run
    await asyncio.wait_for(dispatcher.drain(), timeout=2)
