"""Integration tests for the Redis-backed infrastructure.

Run against a real Redis, not a mock. The behaviours that matter here — a
sliding window that does not permit a boundary burst, an atomic idempotency
claim, ``SCAN``-based invalidation — are properties of Redis itself, and a mock
would assert only that we called the methods we thought we called.

Skipped automatically when Redis is unreachable.
"""

import asyncio

import pytest

from app.infrastructure.queue.client import (
    DEAD_LETTER_KEY,
    DELAYED_KEY,
    STREAM_KEY,
    Job,
    RedisQueue,
    TaskRegistry,
)
from app.infrastructure.queue.worker import Worker
from app.infrastructure.redis.cache import RedisCache
from app.infrastructure.redis.client import (
    build_key,
    close_redis,
    init_redis,
)
from app.infrastructure.redis.rate_limit import check_rate_limit, reset_rate_limit

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client():
    """Connect to Redis, skipping the module when it is unavailable."""
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    yield client

    async for key in client.scan_iter(match=build_key("test") + "*"):
        await client.delete(key)
    await close_redis()


class TestRedisCache:
    async def test_round_trip(self, redis_client) -> None:
        cache = RedisCache()
        await cache.set("test:k", {"a": 1}, ttl_seconds=30)
        assert await cache.get("test:k") == {"a": 1}
        await cache.delete("test:k")

    async def test_miss_returns_none(self, redis_client) -> None:
        assert await RedisCache().get("test:absent") is None

    async def test_expiry_is_applied(self, redis_client) -> None:
        cache = RedisCache()
        await cache.set("test:ttl", "v", ttl_seconds=30)
        ttl = await redis_client.ttl(build_key("test:ttl"))
        assert 0 < ttl <= 30
        await cache.delete("test:ttl")

    async def test_non_positive_ttl_stores_nothing(self, redis_client) -> None:
        """Redis rejects EX 0, so this must not reach the server as-is."""
        cache = RedisCache()
        await cache.set("test:zero", "v", ttl_seconds=0)
        assert await cache.get("test:zero") is None

    async def test_clear_prefix_uses_scan(self, redis_client) -> None:
        cache = RedisCache()
        await cache.set("test:pfx:a", 1, ttl_seconds=30)
        await cache.set("test:pfx:b", 2, ttl_seconds=30)
        await cache.set("test:other", 3, ttl_seconds=30)

        await cache.clear_prefix("test:pfx")

        assert await cache.get("test:pfx:a") is None
        assert await cache.get("test:other") == 3
        await cache.delete("test:other")

    async def test_undecodable_value_is_treated_as_a_miss(self, redis_client) -> None:
        """A value written by an older schema must not raise on read."""
        await redis_client.set(build_key("test:bad"), b"\xff\xfenot json")
        assert await RedisCache().get("test:bad") is None
        await redis_client.delete(build_key("test:bad"))


class TestRateLimit:
    async def test_allows_up_to_the_limit(self, redis_client) -> None:
        await reset_rate_limit("test:rl1")
        for _ in range(3):
            result = await check_rate_limit("test:rl1", limit=3, window_seconds=60)
            assert result.allowed is True

    async def test_rejects_beyond_the_limit(self, redis_client) -> None:
        await reset_rate_limit("test:rl2")
        for _ in range(2):
            await check_rate_limit("test:rl2", limit=2, window_seconds=60)

        result = await check_rate_limit("test:rl2", limit=2, window_seconds=60)
        assert result.allowed is False
        assert result.remaining == 0

    async def test_remaining_counts_down(self, redis_client) -> None:
        await reset_rate_limit("test:rl3")
        first = await check_rate_limit("test:rl3", limit=5, window_seconds=60)
        second = await check_rate_limit("test:rl3", limit=5, window_seconds=60)
        assert first.remaining == 4
        assert second.remaining == 3

    async def test_identities_are_independent(self, redis_client) -> None:
        await reset_rate_limit("test:rl-a")
        await reset_rate_limit("test:rl-b")
        await check_rate_limit("test:rl-a", limit=1, window_seconds=60)

        result = await check_rate_limit("test:rl-b", limit=1, window_seconds=60)
        assert result.allowed is True

    async def test_concurrent_requests_are_counted_atomically(
        self, redis_client
    ) -> None:
        """Two requests in the same millisecond must not collapse into one."""
        await reset_rate_limit("test:rl-conc")
        results = await asyncio.gather(
            *(
                check_rate_limit("test:rl-conc", limit=5, window_seconds=60)
                for _ in range(10)
            )
        )
        assert sum(1 for r in results if r.allowed) == 5

    async def test_headers_are_published(self, redis_client) -> None:
        await reset_rate_limit("test:rl-h")
        result = await check_rate_limit("test:rl-h", limit=10, window_seconds=60)
        headers = result.headers()
        assert headers["X-RateLimit-Limit"] == "10"
        assert headers["X-RateLimit-Remaining"] == "9"


class TestQueue:
    @pytest.fixture(autouse=True)
    async def _clean(self, redis_client):
        for key in (STREAM_KEY, DELAYED_KEY, DEAD_LETTER_KEY):
            await redis_client.delete(build_key(key))
        async for key in redis_client.scan_iter(match=build_key("jobs:idem") + "*"):
            await redis_client.delete(key)

    async def test_enqueue_and_consume(self, redis_client) -> None:
        handled: list[dict] = []
        registry = TaskRegistry()

        @registry.register("ok")
        async def ok(payload: dict) -> None:
            handled.append(payload)

        await RedisQueue().enqueue(Job(name="ok", payload={"x": 1}))

        worker = Worker(registry, name="test", concurrency=2)
        await worker.setup()
        await worker._consume_batch()

        assert handled == [{"x": 1}]

    async def test_idempotency_key_suppresses_duplicates(self, redis_client) -> None:
        queue = RedisQueue()
        await queue.enqueue(Job(name="ok", idempotency_key="dup"))
        await queue.enqueue(Job(name="ok", idempotency_key="dup"))

        assert await redis_client.xlen(build_key(STREAM_KEY)) == 1

    async def test_failure_schedules_a_retry(self, redis_client) -> None:
        registry = TaskRegistry()

        @registry.register("flaky")
        async def flaky(payload: dict) -> None:
            raise RuntimeError("transient")

        await RedisQueue().enqueue(Job(name="flaky", max_retries=3))

        worker = Worker(registry, name="test", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert await redis_client.zcard(build_key(DELAYED_KEY)) == 1

    async def test_exhausted_retries_go_to_the_dead_letter_queue(
        self, redis_client
    ) -> None:
        registry = TaskRegistry()

        @registry.register("always_fails")
        async def always_fails(payload: dict) -> None:
            raise RuntimeError("permanent")

        await RedisQueue().enqueue(Job(name="always_fails", max_retries=0))

        worker = Worker(registry, name="test", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert await redis_client.llen(build_key(DEAD_LETTER_KEY)) == 1

    async def test_unknown_task_is_dead_lettered_not_retried(
        self, redis_client
    ) -> None:
        """An unregistered task can never succeed, so retrying wastes slots."""
        await RedisQueue().enqueue(Job(name="nonexistent"))

        worker = Worker(TaskRegistry(), name="test", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert await redis_client.llen(build_key(DEAD_LETTER_KEY)) == 1
        assert await redis_client.zcard(build_key(DELAYED_KEY)) == 0

    async def test_delayed_jobs_are_not_immediately_runnable(
        self, redis_client
    ) -> None:
        from datetime import timedelta

        await RedisQueue().enqueue(Job(name="ok", delay=timedelta(hours=1)))

        worker = Worker(TaskRegistry(), name="test", concurrency=1)
        await worker.setup()

        assert await worker.promote_delayed() == 0
        assert await redis_client.zcard(build_key(DELAYED_KEY)) == 1

    async def test_due_jobs_are_promoted(self, redis_client) -> None:
        from datetime import timedelta

        await RedisQueue().enqueue(Job(name="ok", delay=timedelta(hours=1)))
        raw = (await redis_client.zrange(build_key(DELAYED_KEY), 0, -1))[0]
        await redis_client.zadd(build_key(DELAYED_KEY), {raw: 0})

        worker = Worker(TaskRegistry(), name="test", concurrency=1)
        await worker.setup()

        assert await worker.promote_delayed() == 1
        assert await redis_client.xlen(build_key(STREAM_KEY)) == 1

    async def test_cancel_removes_a_delayed_job(self, redis_client) -> None:
        from datetime import timedelta

        queue = RedisQueue()
        job_id = await queue.enqueue(Job(name="ok", delay=timedelta(hours=1)))

        assert await queue.cancel(job_id) is True
        assert await redis_client.zcard(build_key(DELAYED_KEY)) == 0

    async def test_cancelling_an_unknown_job_reports_false(self, redis_client) -> None:
        assert await RedisQueue().cancel("no-such-job") is False
