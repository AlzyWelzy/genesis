"""``InMemoryQueue`` must be indistinguishable from ``RedisQueue`` to a caller."""

from datetime import timedelta

import pytest
from redis.asyncio import Redis

from app.infrastructure.queue.client import (
    DELAYED_KEY,
    STREAM_KEY,
    InMemoryQueue,
    Job,
    RedisQueue,
)
from app.infrastructure.redis.client import build_key, close_redis, init_redis

pytestmark = pytest.mark.integration


@pytest.fixture
async def implementations():
    """Both queues, each starting empty."""
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    async def clear() -> None:
        for key in (STREAM_KEY, DELAYED_KEY):
            await client.delete(build_key(key))
        async for key in client.scan_iter(match=build_key("jobs:idem") + "*"):
            await client.delete(key)

    await clear()
    yield InMemoryQueue(), RedisQueue(), client
    await clear()
    await close_redis()


async def _depth(queue: object, client: Redis) -> int:
    """How many jobs a queue is actually holding."""
    if isinstance(queue, InMemoryQueue):
        return len(queue.jobs)
    return await client.xlen(build_key(STREAM_KEY)) + await client.zcard(
        build_key(DELAYED_KEY)
    )


class TestIdempotency:
    async def test_a_repeated_key_enqueues_once_in_both(self, implementations) -> None:
        """The divergence that motivated this suite.

        The fake ignored ``idempotency_key`` entirely — three jobs against the
        fake, one against Redis — so no unit test could observe deduplication at
        all, and the guarantee was untestable rather than merely untested.
        """
        fake, real, client = implementations

        for queue in (fake, real):
            for _ in range(3):
                await queue.enqueue(Job(name="t", idempotency_key="same"))

        assert await _depth(fake, client) == await _depth(real, client) == 1

    async def test_a_suppressed_enqueue_returns_the_winners_id_in_both(
        self, implementations
    ) -> None:
        """A returned ID must identify a job that exists.

        Handing back a freshly minted ID for a job that was never enqueued means
        cancelling it silently does nothing, and storing it against a business
        record points at nothing.
        """
        fake, real, _ = implementations

        for queue in (fake, real):
            first = await queue.enqueue(Job(name="t", idempotency_key="k"))
            second = await queue.enqueue(Job(name="t", idempotency_key="k"))

            assert second == first, f"{type(queue).__name__} returned a phantom ID"

    async def test_distinct_keys_are_not_suppressed_in_either(
        self, implementations
    ) -> None:
        """Deduplication must not become a general-purpose drop."""
        fake, real, client = implementations

        for queue in (fake, real):
            for index in range(3):
                await queue.enqueue(Job(name="t", idempotency_key=f"k{index}"))

        assert await _depth(fake, client) == await _depth(real, client) == 3

    async def test_a_job_without_a_key_is_never_suppressed_in_either(
        self, implementations
    ) -> None:
        """Absent a key, identical jobs are genuinely separate work."""
        fake, real, client = implementations

        for queue in (fake, real):
            for _ in range(3):
                await queue.enqueue(Job(name="t", payload={"same": True}))

        assert await _depth(fake, client) == await _depth(real, client) == 3


class TestEnqueueSemantics:
    async def test_both_return_a_non_empty_identifier(self, implementations) -> None:
        fake, real, _ = implementations

        for queue in (fake, real):
            assert await queue.enqueue(Job(name="t"))

    async def test_enqueue_many_returns_one_id_per_job_in_both(
        self, implementations
    ) -> None:
        fake, real, _ = implementations
        jobs = [Job(name="t"), Job(name="t"), Job(name="t")]

        for queue in (fake, real):
            ids = await queue.enqueue_many(jobs)

            assert len(ids) == len(jobs)
            assert len(set(ids)) == len(jobs)

    async def test_a_delayed_job_is_accepted_by_both(self, implementations) -> None:
        fake, real, client = implementations

        for queue in (fake, real):
            await queue.enqueue(Job(name="t", delay=timedelta(seconds=30)))

        assert await _depth(fake, client) == await _depth(real, client) == 1

    async def test_cancelling_an_unknown_job_is_false_in_both(
        self, implementations
    ) -> None:
        """Neither may raise: a cancel racing a completion is ordinary."""
        fake, real, _ = implementations

        for queue in (fake, real):
            assert await queue.cancel("never-existed") is False


class TestProtocolConformance:
    def test_both_implement_the_whole_queue_protocol(self) -> None:
        """Structural typing means a missing method fails only at the call site."""
        import inspect

        from app.infrastructure.queue.client import Queue

        required = {
            name
            for name, _ in inspect.getmembers(Queue, inspect.isfunction)
            if not name.startswith("_")
        }

        for impl in (InMemoryQueue, RedisQueue):
            available = {
                name
                for name, _ in inspect.getmembers(impl, inspect.isfunction)
                if not name.startswith("_")
            }
            missing = required - available
            assert not missing, f"{impl.__name__} is missing {sorted(missing)}"
