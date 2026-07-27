"""Redis-backed coordination under genuine concurrency.

Each of these guarantees is atomicity provided by Redis — a Lua script, a
``SET NX``, a lock. Sequentially they all appear to work whether or not the
atomicity is there, so they are only meaningfully testable this way.
"""

import asyncio

import pytest

from app.infrastructure.email.client import EmailMessage
from app.infrastructure.email.providers import claim_send
from app.infrastructure.redis.cache import InMemoryCache, get_or_compute, set_cache
from app.infrastructure.redis.rate_limit import check_rate_limit, reset_rate_limit

pytestmark = pytest.mark.integration


class TestRateLimitBoundary:
    async def test_exactly_the_limit_is_admitted(self, live_redis) -> None:
        """The boundary is where a non-atomic limiter leaks.

        Read-then-write in the client means every concurrent request sees the
        same count and all of them proceed. The Lua script is what makes the
        comparison and the record one operation.
        """
        await reset_rate_limit("conc:rl")

        results = await asyncio.gather(
            *(
                check_rate_limit("conc:rl", limit=10, window_seconds=60)
                for _ in range(60)
            )
        )

        assert sum(1 for r in results if r.allowed) == 10

    async def test_rejected_requests_consume_nothing(self, live_redis) -> None:
        """Otherwise a hammering client can never recover."""
        await reset_rate_limit("conc:rl2")

        await asyncio.gather(
            *(
                check_rate_limit("conc:rl2", limit=5, window_seconds=60)
                for _ in range(50)
            )
        )
        entries = await live_redis.zcard(
            __import__(
                "app.infrastructure.redis.client", fromlist=["build_key"]
            ).build_key("ratelimit", "conc:rl2")
        )

        assert entries == 5

    async def test_separate_identities_do_not_interfere(self, live_redis) -> None:
        for identity in ("conc:a", "conc:b"):
            await reset_rate_limit(identity)

        results = await asyncio.gather(
            *(
                check_rate_limit(f"conc:{who}", limit=3, window_seconds=60)
                for who in ("a", "b")
                for _ in range(10)
            )
        )

        assert sum(1 for r in results if r.allowed) == 6


class TestEmailIdempotencyRace:
    async def test_only_one_concurrent_caller_wins_the_claim(self, live_redis) -> None:
        """Two workers handed the same retried job must send once."""
        message = EmailMessage(
            to=["someone@example.com"], subject="race", template="welcome"
        )

        claims = await asyncio.gather(*(claim_send(message) for _ in range(25)))

        assert sum(1 for claimed in claims if claimed) == 1


class TestCacheStampede:
    async def test_one_computation_serves_every_concurrent_caller(
        self, live_redis
    ) -> None:
        """The failure this exists to prevent is worst exactly when it happens.

        A popular key expires, every in-flight request misses at once, and all
        of them run the same expensive query — so the moment the cache is least
        able to help is the moment the database receives the most traffic.
        """
        set_cache(InMemoryCache())
        computations = 0

        async def compute() -> dict:
            nonlocal computations
            computations += 1
            await asyncio.sleep(0.05)
            return {"value": 1}

        results = await asyncio.gather(
            *(get_or_compute("conc:key", compute, ttl_seconds=30) for _ in range(25))
        )

        assert all(result == {"value": 1} for result in results)
        assert computations == 1

    async def test_the_lock_is_released_even_when_computing_fails(
        self, live_redis
    ) -> None:
        """A lock left behind wedges the key for its whole timeout."""
        set_cache(InMemoryCache())

        async def broken() -> dict:
            raise RuntimeError("query failed")

        outcomes = await asyncio.gather(
            *(get_or_compute("conc:fail", broken, ttl_seconds=30) for _ in range(5)),
            return_exceptions=True,
        )
        assert all(isinstance(o, RuntimeError) for o in outcomes)

        # A later caller must still be able to compute.
        async def works() -> dict:
            return {"value": 2}

        assert await get_or_compute("conc:fail", works, ttl_seconds=30) == {"value": 2}


class TestConnectionPoolBackpressure:
    """The pool must make callers wait, never fail them.

    This was the single most consequential bug in the codebase, and it presented
    as three unrelated ones. redis-py's plain ``ConnectionPool`` raises
    ``MaxConnectionsError`` the instant it is exhausted, and every Redis-backed
    control here fails open by design — the rate limiter admits the request, the
    email guard sends the duplicate, the cache reports a miss.

    So past ``max_connections`` concurrent operations they all stopped working
    *together*, silently. For the rate limiter that is an inversion rather than a
    degradation: an attacker needs only enough concurrency to drain the pool, and
    the limit stops applying exactly when it exists to apply.

    Every one of these is invisible below the pool size, which is where a
    sequential test — and any test with modest concurrency — necessarily sits.
    """

    async def test_operations_beyond_the_pool_size_still_succeed(
        self, live_redis
    ) -> None:
        """Backpressure, not failure. Slower is correct; skipped is not."""
        from app.core.config import settings
        from app.infrastructure.redis.client import build_key, get_redis

        beyond = settings.redis.max_connections * 5

        async def touch(index: int) -> str:
            try:
                await get_redis().set(build_key("pool", index), "1", ex=10)
            except Exception as exc:  # noqa: BLE001 - the failure under test
                return type(exc).__name__
            return "ok"

        results = await asyncio.gather(*(touch(i) for i in range(beyond)))

        assert results.count("ok") == beyond

    async def test_the_rate_limiter_still_limits_beyond_the_pool_size(
        self, live_redis
    ) -> None:
        """The security consequence, stated directly.

        A limiter that fails open cannot be defeated by clever requests — only
        by *many* of them, which is precisely what it exists to stop.
        """
        from app.core.config import settings

        await reset_rate_limit("conc:pool-rl")
        attempts = settings.redis.max_connections * 5

        results = await asyncio.gather(
            *(
                check_rate_limit("conc:pool-rl", limit=10, window_seconds=60)
                for _ in range(attempts)
            )
        )

        assert sum(1 for result in results if result.allowed) == 10

    async def test_the_email_guard_still_deduplicates_beyond_the_pool_size(
        self, live_redis
    ) -> None:
        """Failing open here means duplicate password-reset mail under load."""
        from app.core.config import settings

        message = EmailMessage(
            to=["burst@example.com"], subject="burst", template="welcome"
        )
        attempts = settings.redis.max_connections * 5

        claims = await asyncio.gather(*(claim_send(message) for _ in range(attempts)))

        assert sum(1 for claimed in claims if claimed) == 1
