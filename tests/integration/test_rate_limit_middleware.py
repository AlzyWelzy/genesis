"""Rate limiting through the real middleware stack.

Regression coverage for a bug the unit tests could not have caught: raising an
exception inside a ``BaseHTTPMiddleware`` does **not** reach the application's
exception handlers, because those live in ``ExceptionMiddleware`` further down
the stack. The middleware must therefore *return* the 429 response rather than
raise it — otherwise a throttled caller receives a 500 and is told the server
broke when it deliberately refused them.

Only reachable end to end, so it is tested end to end.
"""

import pytest

from app.core.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def throttled_app(fake_cache):
    """An app with a very low rate limit, built against a real Redis."""
    from app.infrastructure.redis.client import init_redis
    from app.main import create_app

    original = (
        settings.rate_limit.enabled,
        settings.rate_limit.anonymous_per_minute,
        settings.app.debug,
    )
    object.__setattr__(settings.rate_limit, "enabled", True)
    object.__setattr__(settings.rate_limit, "anonymous_per_minute", 3)
    object.__setattr__(settings.app, "debug", False)
    try:
        yield create_app(), init_redis
    finally:
        object.__setattr__(settings.rate_limit, "enabled", original[0])
        object.__setattr__(settings.rate_limit, "anonymous_per_minute", original[1])
        object.__setattr__(settings.app, "debug", original[2])


@pytest.fixture
async def throttled_client(throttled_app):
    """A client bound to the throttled app, skipping when Redis is absent."""
    from httpx import ASGITransport, AsyncClient

    app, init_redis = throttled_app
    try:
        await init_redis()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    # Clear every rate-limit key rather than one identity. The identity is
    # derived from the ASGI client address, which the transport chooses, so
    # guessing it couples the test to httpx internals — and a leftover counter
    # from another run silently throttles the first assertion.
    await _clear_rate_limits()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client

    await _clear_rate_limits()
    # The cached client is bound to the event loop that created it, and
    # pytest-asyncio gives each test a fresh loop. Closing here forces the next
    # test to build its own rather than reusing one attached to a dead loop.
    from app.infrastructure.redis.client import close_redis

    await close_redis()


async def _clear_rate_limits() -> None:
    """Delete every rate-limit counter in the namespace."""
    from app.infrastructure.redis.client import build_key, get_redis

    client = get_redis()
    async for key in client.scan_iter(match=build_key("ratelimit") + "*"):
        await client.delete(key)


class TestRateLimitResponses:
    async def test_requests_within_the_limit_pass(self, throttled_client) -> None:
        for _ in range(3):
            response = await throttled_client.get("/api/v1/anything")
            assert response.status_code == 404  # routed, not throttled

    async def test_exceeding_the_limit_returns_429_not_500(
        self, throttled_client
    ) -> None:
        """The bug this file exists for: a raised error became a 500."""
        for _ in range(3):
            await throttled_client.get("/api/v1/anything")

        response = await throttled_client.get("/api/v1/anything")
        assert response.status_code == 429

    async def test_the_429_uses_the_standard_envelope(self, throttled_client) -> None:
        for _ in range(4):
            response = await throttled_client.get("/api/v1/anything")

        error = response.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["message"]

    async def test_retry_after_is_set(self, throttled_client) -> None:
        """Without it, a client's only strategy is to retry blindly."""
        for _ in range(4):
            response = await throttled_client.get("/api/v1/anything")

        assert int(response.headers["Retry-After"]) > 0

    async def test_rate_limit_headers_are_published_on_success(
        self, throttled_client
    ) -> None:
        """A well-behaved client should slow down before being rejected."""
        response = await throttled_client.get("/api/v1/anything")

        assert response.headers["X-RateLimit-Limit"] == "3"
        assert int(response.headers["X-RateLimit-Remaining"]) < 3

    async def test_health_probes_are_exempt(self, throttled_client) -> None:
        """A throttled probe gets healthy pods killed by the orchestrator."""
        for _ in range(10):
            response = await throttled_client.get("/live")
            assert response.status_code == 200


class TestPerRouteLimits:
    """Tighter limits declared per route, which were previously dead code.

    ``register_endpoint_limit``, ``limit_for_route``, ``EndpointLimit.burst``
    and ``check_token_bucket`` were all written, documented and unreachable: the
    middleware only ever applied the global limit. A feature declaring a tighter
    allowance for an expensive endpoint got no effect at all, silently — which
    is worse than having no mechanism, because the protection is believed to be
    in place.

    The obstacle was real. Middleware runs above the router, so
    ``scope["route"]`` is empty at the point the limiter needs it; the template
    has to be resolved by matching the scope against the app's routes first.
    """

    @pytest.fixture
    def endpoint_app(self, throttled_app):
        """An app with two routes, one carrying a tighter limit."""
        from app.infrastructure.redis.rate_limit import (
            ENDPOINT_LIMITS,
            EndpointLimit,
            register_endpoint_limit,
        )

        app, init_redis = throttled_app

        @app.get("/_t/expensive")
        async def expensive() -> dict:
            return {"ok": True}

        @app.get("/_t/cheap")
        async def cheap() -> dict:
            return {"ok": True}

        register_endpoint_limit("/_t/expensive", EndpointLimit(limit=1))
        yield app, init_redis
        ENDPOINT_LIMITS.clear()

    @pytest.fixture
    async def endpoint_client(self, endpoint_app):
        from httpx import ASGITransport, AsyncClient

        app, init_redis = endpoint_app
        try:
            await init_redis()
        except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
            pytest.skip(f"Redis unavailable: {exc}")

        await _clear_rate_limits()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
        await _clear_rate_limits()

        from app.infrastructure.redis.client import close_redis

        await close_redis()

    async def test_the_tighter_limit_is_enforced(self, endpoint_client) -> None:
        """One request allowed, the second refused — despite a global limit of 3."""
        assert (await endpoint_client.get("/_t/expensive")).status_code == 200
        assert (await endpoint_client.get("/_t/expensive")).status_code == 429

    async def test_the_published_limit_is_the_route_s_own(
        self, endpoint_client
    ) -> None:
        """A client reading the header must see the limit that applies to it."""
        response = await endpoint_client.get("/_t/expensive")
        assert response.headers["X-RateLimit-Limit"] == "1"

    async def test_other_routes_keep_the_global_limit(self, endpoint_client) -> None:
        """A tight limit on one endpoint must not throttle everything else."""
        for _ in range(3):
            assert (await endpoint_client.get("/_t/cheap")).status_code == 200

    async def test_exhausting_a_route_does_not_block_other_routes(
        self, endpoint_client
    ) -> None:
        """Per-route counters must be keyed separately, or they share a budget."""
        await endpoint_client.get("/_t/expensive")
        assert (await endpoint_client.get("/_t/expensive")).status_code == 429

        assert (await endpoint_client.get("/_t/cheap")).status_code == 200

    async def test_an_unmatched_path_still_falls_back_to_the_global_limit(
        self, endpoint_client
    ) -> None:
        """An unrecognised path must be throttled, never exempt."""
        for _ in range(3):
            await endpoint_client.get("/api/v1/no-such-route")

        response = await endpoint_client.get("/api/v1/no-such-route")
        assert response.status_code == 429


class TestBurstLimits:
    """The token-bucket path, for endpoints where a short burst is legitimate."""

    @pytest.fixture
    def burst_app(self, throttled_app):
        from app.infrastructure.redis.rate_limit import (
            ENDPOINT_LIMITS,
            EndpointLimit,
            register_endpoint_limit,
        )

        app, init_redis = throttled_app

        @app.get("/_t/sync")
        async def sync_endpoint() -> dict:
            return {"ok": True}

        # A client syncing five records at startup then going quiet is exactly
        # the shape a sliding window handles badly.
        register_endpoint_limit(
            "/_t/sync", EndpointLimit(limit=60, window_seconds=60, burst=5)
        )
        yield app, init_redis
        ENDPOINT_LIMITS.clear()

    @pytest.fixture
    async def burst_client(self, burst_app):
        from httpx import ASGITransport, AsyncClient

        from app.infrastructure.redis.client import build_key, close_redis, get_redis

        app, init_redis = burst_app
        try:
            await init_redis()
        except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
            pytest.skip(f"Redis unavailable: {exc}")

        async for key in get_redis().scan_iter(match=build_key("bucket") + "*"):
            await get_redis().delete(key)
        await _clear_rate_limits()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client

        async for key in get_redis().scan_iter(match=build_key("bucket") + "*"):
            await get_redis().delete(key)
        await close_redis()

    async def test_the_whole_burst_is_permitted_back_to_back(
        self, burst_client
    ) -> None:
        """The point of a bucket: spend it all at once, then refill."""
        for _ in range(5):
            assert (await burst_client.get("/_t/sync")).status_code == 200

    async def test_the_request_past_the_burst_is_refused(self, burst_client) -> None:
        for _ in range(5):
            await burst_client.get("/_t/sync")

        assert (await burst_client.get("/_t/sync")).status_code == 429
