"""A blocking Redis read must not out-wait the client's own socket deadline.

Why this exists
---------------
``XREADGROUP`` with ``block=5000`` held the connection for five seconds while
the client applied a five-second socket read timeout. The client therefore gave
up first — not occasionally, but on **every idle poll**.

The worker's loop caught the resulting ``redis.TimeoutError``, logged a full
traceback and slept a second. So an idle worker emitted an ERROR every few
seconds, forever: the exact opposite of the "an idle worker blocks rather than
spinning" behaviour the code was written to have, and more than enough noise to
bury a genuine failure.

Invisible to a sequential test that enqueues a job first, because then the read
returns immediately and never reaches its block duration. It only appears when
the queue is *empty*, which is the state a healthy worker spends most of its life
in.
"""

import asyncio
import contextlib

import pytest

from app.core.config import settings
from app.infrastructure.queue.client import TaskRegistry
from app.infrastructure.queue.worker import Worker
from app.infrastructure.redis.client import blocking_read_ms
from app.infrastructure.redis.pubsub import RedisStreamsPubSub

pytestmark = pytest.mark.integration


class TestBlockingReadBudget:
    def test_the_block_is_shorter_than_the_socket_timeout(self) -> None:
        """The invariant. Equal is not good enough — it is the failing case."""
        socket_timeout_ms = settings.redis.socket_timeout_seconds * 1000
        block = blocking_read_ms()

        assert block is not None
        assert block < socket_timeout_ms

    def test_zero_is_never_returned(self) -> None:
        """``BLOCK 0`` means *block forever*, inverting the whole intent."""
        for timeout_seconds in (0.001, 0.01, 0.5, 1.0, 5.0, 60.0):
            original = settings.redis.socket_timeout_seconds
            object.__setattr__(
                settings.redis, "socket_timeout_seconds", timeout_seconds
            )
            try:
                assert blocking_read_ms() != 0
            finally:
                object.__setattr__(settings.redis, "socket_timeout_seconds", original)

    @pytest.mark.parametrize("timeout_seconds", [0.5, 1.0, 5.0, 30.0, 120.0])
    def test_the_invariant_holds_at_any_configured_timeout(
        self, timeout_seconds: float
    ) -> None:
        """Derived, not hard-coded, so lowering the setting stays safe.

        A literal ``5000`` was correct only for the default and silently wrong
        for every other value someone might configure.
        """
        original = settings.redis.socket_timeout_seconds
        object.__setattr__(settings.redis, "socket_timeout_seconds", timeout_seconds)
        try:
            block = blocking_read_ms()
            assert block is None or block < timeout_seconds * 1000
        finally:
            object.__setattr__(settings.redis, "socket_timeout_seconds", original)

    @pytest.mark.parametrize("timeout_seconds", [0.001, 0.01, 0.5, 1.0])
    def test_a_tiny_timeout_still_yields_a_usable_block(
        self, timeout_seconds: float
    ) -> None:
        """No constant floor, deliberately.

        A floor is the tempting addition and it is wrong: any constant lower
        bound is itself a hard-coded duration, and it re-creates this exact bug
        as soon as someone configures a timeout below twice that constant. The
        first version of this fix used a 500ms floor and failed at a 0.5s
        timeout — caught by parametrising the setting rather than testing only
        the default.
        """
        original = settings.redis.socket_timeout_seconds
        object.__setattr__(settings.redis, "socket_timeout_seconds", timeout_seconds)
        try:
            block = blocking_read_ms()
            # `None` means "do not block", which is the only honest answer when
            # the budget is smaller than the smallest expressible wait. `0` is
            # never returned: to Redis that means *block forever*.
            assert block is None or 1 <= block < timeout_seconds * 1000
        finally:
            object.__setattr__(settings.redis, "socket_timeout_seconds", original)


class TestIdleWorker:
    async def test_an_idle_poll_returns_instead_of_raising(self, live_redis) -> None:
        """The bug, stated directly: an empty queue must be uneventful."""
        worker = Worker(TaskRegistry(), name="idle-probe", concurrency=1)
        await worker.setup()

        for _ in range(3):
            await worker._consume_batch()

    async def test_an_idle_poll_logs_nothing(self, live_redis, caplog) -> None:
        """Log noise on the idle path buries the errors that matter."""
        worker = Worker(TaskRegistry(), name="idle-quiet", concurrency=1)
        await worker.setup()

        with caplog.at_level("WARNING"):
            await worker._consume_batch()

        assert caplog.records == []


class TestIdleStreamSubscriber:
    async def test_an_idle_stream_read_does_not_raise(self, live_redis) -> None:
        """``RedisStreamsPubSub`` carried the same hard-coded block.

        Waited on with a timeout longer than one blocking read but shorter than
        the socket deadline: if the read raised, the error surfaces here, and if
        it merely blocks — the correct behaviour on an empty stream — the wait
        times out cleanly.
        """
        subscription = RedisStreamsPubSub().subscribe("concurrency-probe")
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(anext(subscription), timeout=0.75)
        finally:
            await subscription.aclose()
