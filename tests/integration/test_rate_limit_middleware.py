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


# The per-route and burst tests that used to live here have moved to
# `tests/concurrency/test_redis_concurrency.py::TestPerRouteLimits`.
#
# They belonged to a middleware implementation that resolved the route template
# from `app.routes` before routing — which cannot work, because FastAPI does not
# flatten `include_router`. These tests passed only because they registered
# their routes with `@app.get`, producing a top-level route that the broken
# resolver *could* see. No feature mounts a route that way, so the tests were
# green and the feature was inert.
#
# Per-route limiting is now a router dependency, and its tests mount routes the
# way a feature does: a router, included into a version router, included into
# the app.
