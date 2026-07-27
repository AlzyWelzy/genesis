"""Integration tests for the Redis-backed infrastructure.

Run against a real Redis, not a mock. The behaviours that matter here — a
sliding window that does not permit a boundary burst, an atomic idempotency
claim, ``SCAN``-based invalidation — are properties of Redis itself, and a mock
would assert only that we called the methods we thought we called.

Skipped automatically when Redis is unreachable.
"""

import asyncio

import pytest

from app.infrastructure.email.client import EmailMessage
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


class TestConnectionLifecycle:
    """The startup path, which decides whether the process is allowed to serve."""

    async def test_a_failed_connection_leaves_nothing_cached(self) -> None:
        """A failed init must not poison the process-wide client.

        Assigning the client before pinging looks equivalent to assigning after
        and is not: the failed client stays cached, so the *next* ``init_redis``
        takes the "already initialised" early return and reports success without
        ever pinging. Startup then proceeds against a Redis that is not there,
        and the failure resurfaces later, at the point of use, with nothing
        connecting it back to the cause.
        """
        from app.core.config import settings
        from app.infrastructure.redis import client as redis_client_module

        await close_redis()
        original = settings.redis.url
        # A port nothing listens on, so connecting fails fast.
        object.__setattr__(settings.redis, "url", "redis://127.0.0.1:1/0")
        try:
            with pytest.raises(Exception, match=r".*"):
                await init_redis()

            assert redis_client_module._client is None
            with pytest.raises(RuntimeError, match="not initialised"):
                redis_client_module.get_redis()
            assert await redis_client_module.check_redis_health() is False

            # And the second attempt must genuinely retry, not report success.
            with pytest.raises(Exception, match=r".*"):
                await init_redis()
        finally:
            object.__setattr__(settings.redis, "url", original)
            await close_redis()


class TestEmailIdempotencyClaim:
    """The guard must not become the reason a message is never delivered.

    The claim is taken *before* the transport runs, so a send that then fails
    would leave the key in place for the whole idempotency window — suppressing
    the queue retry that is the entire reason the guard exists. A transient SMTP
    error stops being "the user gets a duplicate" and becomes "the password
    reset never arrives", silently, for hours.
    """

    @pytest.fixture(autouse=True)
    async def _clear_claims(self, redis_client):
        """Drop any claim left by a previous run.

        The claims carry a 24-hour TTL and live outside the ``test:`` prefix the
        session fixture sweeps, so without this the first assertion in each test
        depends on whether the suite ran earlier today.
        """
        async for key in redis_client.scan_iter(match=build_key("email:sent") + "*"):
            await redis_client.delete(key)

    @staticmethod
    def _message(subject: str) -> EmailMessage:
        return EmailMessage(
            to=["someone@example.com"], subject=subject, template="welcome"
        )

    async def test_a_claim_suppresses_a_duplicate(self, redis_client) -> None:
        from app.infrastructure.email.providers import claim_send

        message = self._message("test claim dedupe")
        assert await claim_send(message) is True
        assert await claim_send(message) is False

    async def test_releasing_a_claim_allows_the_retry(self, redis_client) -> None:
        from app.infrastructure.email.providers import claim_send, release_send_claim

        message = self._message("test claim release")
        assert await claim_send(message) is True

        await release_send_claim(message)
        assert await claim_send(message) is True

    async def test_a_failed_send_releases_its_claim(self, redis_client) -> None:
        """End to end through the provider, with the transport failing."""
        from app.infrastructure.email.providers import SMTPEmailProvider, claim_send

        message = self._message("test failed send")

        class FailingClient:
            async def connect(self) -> None: ...
            async def quit(self) -> None: ...

            async def send_message(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("connection reset")

        provider = SMTPEmailProvider.__new__(SMTPEmailProvider)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "app.infrastructure.email.providers.aiosmtplib.SMTP",
                lambda **_kwargs: FailingClient(),
            )
            with pytest.raises(Exception, match="Email delivery failed"):
                await provider.send_batch([message])

        # The retry must be able to claim it again. Without the release this
        # returns False and the message is lost for the idempotency window.
        assert await claim_send(message) is True


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

    async def test_a_rejected_request_does_not_consume_allowance(
        self, redis_client
    ) -> None:
        """Otherwise a client that retries while blocked never recovers.

        Recording the rejected attempt pushes the caller's own window forward on
        every retry, so it stays locked out until it stops entirely for a full
        window — which a polling client never does. The lockout is permanent and
        looks, from the outside, like the limiter is simply broken.

        Verified through the sorted set rather than through a later decision,
        because the window here is 60 seconds: the state is the evidence.
        """
        await reset_rate_limit("test:rl-retry")
        for _ in range(2):
            await check_rate_limit("test:rl-retry", limit=2, window_seconds=60)

        for _ in range(5):
            rejected = await check_rate_limit(
                "test:rl-retry", limit=2, window_seconds=60
            )
            assert rejected.allowed is False

        assert await redis_client.zcard(build_key("ratelimit", "test:rl-retry")) == 2

    async def test_a_short_window_frees_up_despite_continuous_retries(
        self, redis_client
    ) -> None:
        """The end-to-end consequence: a hammering client is served again."""
        await reset_rate_limit("test:rl-recover")
        assert (
            await check_rate_limit("test:rl-recover", limit=1, window_seconds=1)
        ).allowed

        # Retry throughout and past the window, as a real client would. One of
        # these must succeed; under the old behaviour every retry re-armed the
        # window and none ever would.
        served = []
        for _ in range(8):
            await asyncio.sleep(0.2)
            result = await check_rate_limit(
                "test:rl-recover", limit=1, window_seconds=1
            )
            served.append(result.allowed)

        assert any(served)

    async def test_reset_reflects_the_oldest_entry_not_a_whole_window(
        self, redis_client
    ) -> None:
        """``Retry-After`` must say when allowance returns, not the window length.

        A flat window length tells a client to wait far longer than it needs to,
        which for an interactive caller reads as a much harsher limit than the
        one configured.
        """
        await reset_rate_limit("test:rl-reset")
        await check_rate_limit("test:rl-reset", limit=1, window_seconds=10)
        await asyncio.sleep(1.1)

        rejected = await check_rate_limit("test:rl-reset", limit=1, window_seconds=10)
        assert rejected.allowed is False
        assert rejected.reset_after < 10

    async def test_reset_is_never_zero_while_blocked(self, redis_client) -> None:
        """A ``Retry-After: 0`` invites an immediate retry that is also rejected."""
        await reset_rate_limit("test:rl-reset0")
        await check_rate_limit("test:rl-reset0", limit=1, window_seconds=1)

        rejected = await check_rate_limit("test:rl-reset0", limit=1, window_seconds=1)
        assert rejected.allowed is False
        assert rejected.reset_after >= 1


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
