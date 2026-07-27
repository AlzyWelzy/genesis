"""Two workers on one stream must run each job exactly once.

Why this matters
----------------
Running two workers is the normal deployment, and "handlers must be idempotent"
is a real instruction in this codebase — but at-least-once is a recovery
guarantee for crashes, not a licence for the happy path to double-deliver.
A consumer group is what keeps steady-state delivery single. Sequentially, a
worker passes whether or not the group is used at all.
"""

import asyncio

import pytest

from app.infrastructure.queue.client import (
    DEAD_LETTER_KEY,
    Job,
    RedisQueue,
    TaskRegistry,
)
from app.infrastructure.queue.worker import Worker
from app.infrastructure.redis.client import build_key

pytestmark = pytest.mark.integration


class TestConcurrentWorkers:
    async def test_each_job_runs_exactly_once(self, live_redis) -> None:
        """The steady-state guarantee a consumer group provides."""
        handled: list[str] = []
        lock = asyncio.Lock()

        registry = TaskRegistry()

        @registry.register("conc.once")
        async def handler(payload: dict) -> None:
            await asyncio.sleep(0)
            async with lock:
                handled.append(payload["n"])

        queue = RedisQueue()
        for n in range(30):
            await queue.enqueue(Job(name="conc.once", payload={"n": str(n)}))

        workers = [Worker(registry, name=f"w{i}", concurrency=4) for i in range(3)]
        for worker in workers:
            await worker.setup()
        await asyncio.gather(*(worker._consume_batch() for worker in workers))

        assert len(handled) == len(set(handled)), "a job ran twice"

    async def test_every_job_is_eventually_run(self, live_redis) -> None:
        """Splitting the work must not drop any of it."""
        handled: list[str] = []
        lock = asyncio.Lock()

        registry = TaskRegistry()

        @registry.register("conc.all")
        async def handler(payload: dict) -> None:
            async with lock:
                handled.append(payload["n"])

        queue = RedisQueue()
        expected = [str(n) for n in range(30)]
        for n in expected:
            await queue.enqueue(Job(name="conc.all", payload={"n": n}))

        workers = [Worker(registry, name=f"w{i}", concurrency=4) for i in range(3)]
        for worker in workers:
            await worker.setup()
        for _ in range(6):
            await asyncio.gather(*(w._consume_batch() for w in workers))

        assert sorted(handled) == sorted(expected)

    async def test_a_failing_job_does_not_take_its_batch_with_it(
        self, live_redis
    ) -> None:
        """One poison payload must not strand the jobs beside it."""
        succeeded: list[str] = []

        registry = TaskRegistry()

        @registry.register("conc.mixed")
        async def handler(payload: dict) -> None:
            if payload["n"] == "poison":
                raise ValueError("cannot handle this one")
            succeeded.append(payload["n"])

        queue = RedisQueue()
        for n in ["a", "poison", "b", "c"]:
            await queue.enqueue(Job(name="conc.mixed", payload={"n": n}))

        worker = Worker(registry, name="w", concurrency=4)
        await worker.setup()
        await worker._consume_batch()

        assert sorted(succeeded) == ["a", "b", "c"]


class TestIdempotentEnqueue:
    async def test_concurrent_producers_create_one_job(self, live_redis) -> None:
        """``SET NX`` is what makes this atomic.

        Two replicas reacting to the same webhook is the ordinary case, not an
        edge case, and both will try to enqueue.
        """
        queue = RedisQueue()
        await asyncio.gather(
            *(
                queue.enqueue(
                    Job(
                        name="conc.idem",
                        payload={"n": str(i)},
                        idempotency_key="same-key",
                    )
                )
                for i in range(20)
            )
        )

        depth = await live_redis.xlen(build_key("jobs"))
        assert depth == 1

    async def test_different_keys_are_not_suppressed(self, live_redis) -> None:
        """Deduplication must not become a general-purpose drop."""
        queue = RedisQueue()
        await asyncio.gather(
            *(
                queue.enqueue(
                    Job(name="conc.idem2", payload={}, idempotency_key=f"key-{i}")
                )
                for i in range(10)
            )
        )

        assert await live_redis.xlen(build_key("jobs")) == 10


class TestDeadLettering:
    async def test_an_exhausted_job_lands_in_the_dead_letter_queue_once(
        self, live_redis
    ) -> None:
        """A silently growing DLQ is bad; a doubly-populated one is worse."""
        registry = TaskRegistry()

        @registry.register("conc.dead")
        async def handler(_payload: dict) -> None:
            raise ValueError("always fails")

        await RedisQueue().enqueue(Job(name="conc.dead", max_retries=0))

        workers = [Worker(registry, name=f"w{i}", concurrency=2) for i in range(3)]
        for worker in workers:
            await worker.setup()
        await asyncio.gather(*(w._consume_batch() for w in workers))

        assert await live_redis.llen(build_key(DEAD_LETTER_KEY)) == 1
