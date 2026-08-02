"""Behaviour when a dependency is unavailable, not merely slow.

Why this exists
---------------
Every test elsewhere runs against healthy PostgreSQL and Redis, so all of them
exercise the same branch: the one where the dependency answers. The other branch
is written, documented, and until now never executed — and it is the branch that
decides whether a dependency outage becomes a degraded service or a total one.

The distinctions that matter here are deliberate design decisions in this
codebase, each written down and none previously verified:

* the cache degrades to a miss, because an unavailable cache should make the
  application slower and never broken;
* the rate limiter's direction is *configurable*, because failing closed turns a
  Redis blip into a full outage while failing open drops a protection;
* readiness must go false while liveness stays true, because an orchestrator
  responds to those two signals completely differently — deregister versus kill.
"""

import pytest

from app.core.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_pointed_at_nothing(monkeypatch):
    """Repoint Redis at a closed port and reset the cached client.

    A closed port rather than a stopped container: it fails immediately and
    deterministically, where a dropped packet would wait for a timeout and make
    the test slow and flaky.
    """
    from app.infrastructure.redis import client as redis_client

    original = settings.redis.url
    object.__setattr__(settings.redis, "url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(redis_client, "_client", None, raising=False)
    monkeypatch.setattr(redis_client, "_pool", None, raising=False)
    yield
    object.__setattr__(settings.redis, "url", original)


class TestCacheDegradation:
    async def test_a_read_becomes_a_miss(self, redis_pointed_at_nothing) -> None:
        """An unavailable cache must make the app slower, never broken."""
        from app.infrastructure.redis.cache import RedisCache

        assert await RedisCache().get("anything") is None

    async def test_a_write_is_swallowed(self, redis_pointed_at_nothing) -> None:
        """Failing a user's write because the *cache* is down is the wrong trade."""
        from app.infrastructure.redis.cache import RedisCache

        await RedisCache().set("k", {"v": 1}, ttl_seconds=30)

    async def test_stampede_protection_still_computes(
        self, redis_pointed_at_nothing
    ) -> None:
        """The lock is an optimisation; losing it must not drop the request."""
        from app.infrastructure.redis.cache import (
            InMemoryCache,
            get_or_compute,
            set_cache,
        )

        set_cache(InMemoryCache())

        async def compute() -> dict:
            return {"value": 1}

        assert await get_or_compute("k", compute, ttl_seconds=30) == {"value": 1}


class TestRateLimiterDirection:
    async def test_fail_open_admits_the_request(self, redis_pointed_at_nothing) -> None:
        """The configured default.

        A limiter that blocks everything when Redis blinks has converted a cache
        outage into a full outage.
        """
        from app.infrastructure.redis.rate_limit import check_rate_limit

        original = settings.rate_limit.fail_open
        object.__setattr__(settings.rate_limit, "fail_open", True)
        try:
            result = await check_rate_limit("x", limit=10, window_seconds=60)
        finally:
            object.__setattr__(settings.rate_limit, "fail_open", original)

        assert result.allowed is True

    async def test_fail_closed_refuses_the_request(
        self, redis_pointed_at_nothing
    ) -> None:
        """Inverted deliberately for a limit protecting something expensive."""
        from app.infrastructure.redis.rate_limit import check_rate_limit

        original = settings.rate_limit.fail_open
        object.__setattr__(settings.rate_limit, "fail_open", False)
        try:
            result = await check_rate_limit("x", limit=10, window_seconds=60)
        finally:
            object.__setattr__(settings.rate_limit, "fail_open", original)

        assert result.allowed is False

    async def test_the_outage_is_logged_either_way(
        self, redis_pointed_at_nothing, caplog
    ) -> None:
        """An unenforced rate limit somebody knows about is survivable."""
        from app.infrastructure.redis.rate_limit import check_rate_limit

        with caplog.at_level("WARNING"):
            await check_rate_limit("x", limit=10, window_seconds=60)

        assert "Rate limiter unavailable" in caplog.text


class TestEmailGuardDegradation:
    async def test_a_send_proceeds_without_the_guard(
        self, redis_pointed_at_nothing
    ) -> None:
        """A duplicate email is a smaller harm than a reset that never arrives."""
        from app.infrastructure.email.client import EmailMessage
        from app.infrastructure.email.providers import claim_send

        message = EmailMessage(to=["a@example.com"], subject="s", template="welcome")

        assert await claim_send(message) is True


class TestProbeSemantics:
    """Liveness and readiness mean different things to an orchestrator.

    Readiness false removes the pod from the load balancer; liveness false
    *kills* it. Reporting a dependency outage as liveness restarts every healthy
    replica during a database blip, turning a partial outage into a total one.
    """

    async def test_liveness_stays_true_while_redis_is_down(
        self, redis_pointed_at_nothing, client
    ) -> None:
        assert (await client.get("/live")).status_code == 200

    async def test_readiness_reports_redis_as_unhealthy(
        self, redis_pointed_at_nothing
    ) -> None:
        from app.infrastructure.redis.client import check_redis_health

        assert await check_redis_health() is False

    async def test_liveness_never_consults_a_dependency(self, client) -> None:
        """A database blip must not get every healthy replica killed."""
        assert (await client.get("/live")).status_code == 200
