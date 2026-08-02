"""Resources released after use, not merely acquired correctly.

Why this exists
---------------
A leak is invisible in every functional test. The operation returns the right
answer, the assertions pass, and the connection, task or file handle is simply
never given back. The failure arrives hours later as pool exhaustion or a memory
ceiling, in a process that has been serving correctly the whole time — and the
traceback points at whichever unlucky request found the pool empty, not at the
code that drained it.

That makes leaks a distinct dimension from correctness, concurrency and input
handling, and one no other suite here reaches: each of those runs an operation a
handful of times and asserts what it *returned*. These run it many times and
assert what it *released*.
"""

import asyncio
import gc

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text

from app.infrastructure.database.session import session_factory, session_scope
from app.infrastructure.redis.cache import RedisCache
from app.infrastructure.redis.client import build_key, get_redis

pytestmark = pytest.mark.integration


def _checked_out(engine: object) -> int:
    """Connections the SQLAlchemy pool has handed out and not got back.

    Read defensively: ``checkedout`` exists on every pool implementation used
    here but is not part of the abstract ``Pool`` type the stubs expose.
    """
    return getattr(engine.pool, "checkedout", lambda: 0)()  # ty: ignore[unresolved-attribute]


def _in_use(client: object) -> int:
    """Redis connections currently checked out of the pool."""
    pool = client.connection_pool  # ty: ignore[unresolved-attribute]
    return len(getattr(pool, "_in_use_connections", ()) or ())


def _live_tasks() -> set[asyncio.Task]:
    """Tasks still running, excluding the one calling this."""
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}


class TestRedisConnections:
    async def test_repeated_operations_do_not_exhaust_the_pool(
        self, live_redis
    ) -> None:
        """Many operations must reuse connections, not accumulate them.

        A connection returned to the pool is invisible in the result; one that
        is *not* returned is invisible until the pool runs dry.
        """
        cache = RedisCache()
        for i in range(200):
            await cache.set(f"lifecycle:{i}", {"i": i}, ttl_seconds=10)
            await cache.get(f"lifecycle:{i}")

        assert _in_use(get_redis()) == 0

    async def test_a_failing_operation_still_returns_its_connection(
        self, live_redis
    ) -> None:
        """The error path is where a release is most often forgotten."""
        client = get_redis()
        for _ in range(50):
            # WRONGTYPE: a real Redis error raised mid-command.
            await client.lpush(build_key("lifecycle:list"), "x")
            with pytest.raises(RedisError):
                await client.incr(build_key("lifecycle:list"))  # ty: ignore[invalid-await]

        assert _in_use(client) == 0

    async def test_a_cancelled_operation_returns_its_connection(
        self, live_redis
    ) -> None:
        """A client disconnecting mid-request cancels the task handling it."""
        client = get_redis()

        for _ in range(20):
            task = asyncio.create_task(
                client.blpop([build_key("lifecycle:none")], 5)  # ty: ignore[invalid-argument-type]
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await asyncio.sleep(0.05)
        assert _in_use(client) == 0


class TestDatabaseSessions:
    async def test_session_scope_returns_every_connection(self, shared_engine) -> None:
        """``session_scope`` is used by workers and scripts in long loops."""
        from app.infrastructure.database.session import engine

        for _ in range(60):
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))

        assert _checked_out(engine) == 0

    async def test_a_failed_transaction_returns_its_connection(
        self, shared_engine
    ) -> None:
        """Rollback is the path a leak hides on."""
        from app.infrastructure.database.session import engine

        async def boom() -> None:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("failed unit of work")

        for _ in range(40):
            with pytest.raises(RuntimeError, match="failed unit of work"):
                await boom()

        assert _checked_out(engine) == 0

    async def test_an_unused_session_is_still_closed(self, shared_engine) -> None:
        """Opening a session and doing nothing must not hold a connection."""
        from app.infrastructure.database.session import engine

        for _ in range(40):
            async with session_factory():
                pass

        assert _checked_out(engine) == 0


class TestBackgroundTasks:
    async def test_no_task_is_left_running_after_a_relay_pass(
        self, live_redis, shared_engine
    ) -> None:
        """A loop that spawns a task per pass floods the scheduler slowly."""
        from app.infrastructure.outbox.relay import OutboxRelay

        async def publisher(_message: object) -> None:
            return

        before = _live_tasks()
        for _ in range(10):
            await OutboxRelay(publisher).run_once()
        gc.collect()

        assert _live_tasks() - before == set()

    async def test_a_stream_subscription_closes_cleanly(self, live_redis) -> None:
        """``RedisStreamsPubSub.subscribe`` opens a dedicated connection.

        An abandoned subscription that never closes leaks one connection per
        subscriber, which on a reconnect loop is a leak per reconnect.
        """
        from app.infrastructure.redis.pubsub import RedisStreamsPubSub

        before = _live_tasks()
        for _ in range(5):
            subscription = RedisStreamsPubSub().subscribe("lifecycle-probe")
            try:
                await asyncio.wait_for(anext(subscription), timeout=0.3)
            except TimeoutError, StopAsyncIteration:
                pass
            finally:
                await subscription.aclose()

        gc.collect()
        assert _live_tasks() - before == set()
