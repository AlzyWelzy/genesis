"""Redis connection pool and client accessor.

Why this file exists
--------------------
Redis serves four unrelated concerns here — caching, pub/sub, the job queue and
rate limiting — and each would otherwise build its own connection pool. This
module owns the single pool for the process, so connection limits are
predictable and shutdown closes everything.

It exposes a client and a key builder, nothing more. Semantics (what a key
means, how long it lives, what a channel carries) belong to the modules that
own them.

Namespacing
-----------
Every key goes through :func:`build_key`. A shared Redis instance without a
namespace is how a staging deploy silently reads production cache entries — and
that failure is invisible, because a cache hit looks identical either way.
"""

from typing import Any

from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis() -> Redis:
    """Create the connection pool and verify connectivity.

    Called once from the application lifespan. Pinging here turns a
    misconfigured Redis into a startup failure rather than a surprise on the
    first request that happens to need a cache.

    The client is cached for the process, and is **bound to the event loop that
    created it**. That is correct for a server, which has one loop for its whole
    life, but it means anything creating a fresh loop — a test suite, a worker
    restart — must call :func:`close_redis` first, or the next use fails with
    "Event loop is closed".

    Returns:
        The connected client.
    """
    # Process-wide singleton by design; see the module docstring.
    global _pool, _client

    if _client is not None:
        return _client

    pool = ConnectionPool.from_url(
        str(settings.redis.url),
        max_connections=settings.redis.max_connections,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_timeout_seconds,
        decode_responses=False,
    )
    client = Redis(connection_pool=pool)

    # Published to the module globals only after the ping succeeds. Assigning
    # first and pinging second looks equivalent and is not: a failed ping leaves
    # a broken client cached, so the *next* call takes the early return above
    # and reports success without ever pinging. Startup then proceeds against a
    # Redis that is not there, and the failure resurfaces later, at the point of
    # use, with nothing connecting it to the cause.
    try:
        await client.ping()
    except BaseException:
        await client.aclose()
        await pool.aclose()
        raise

    _pool, _client = pool, client
    logger.info(
        "Redis connected",
        extra={"max_connections": settings.redis.max_connections},
    )
    return client


def get_redis() -> Redis:
    """Return the initialised client.

    Raises:
        RuntimeError: When called before the lifespan initialised the pool.
            A programming error, and worth failing loudly on.
    """
    if _client is None:
        raise RuntimeError("Redis is not initialised; call init_redis() first.")
    return _client


async def check_redis_health() -> bool:
    """Verify Redis answers a ping. Used by the readiness probe."""
    if _client is None:
        return False
    try:
        await _client.ping()
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        return False
    return True


async def close_redis() -> None:
    """Close the pool. Called from the application lifespan on shutdown."""
    global _pool, _client  # noqa: PLW0603 - process-wide singleton by design

    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def build_key(*parts: Any) -> str:
    """Join key parts under the configured namespace.

    Every key written through this package must go through here.

    Args:
        *parts: Key segments, joined with ``:``.

    Returns:
        The fully namespaced key.
    """
    return ":".join((settings.redis.key_prefix, *(str(part) for part in parts)))
