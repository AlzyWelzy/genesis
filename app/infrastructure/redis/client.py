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

    Returns:
        The connected client.
    """
    global _pool, _client  # noqa: PLW0603 - process-wide singleton by design

    if _client is not None:
        return _client

    _pool = ConnectionPool.from_url(
        str(settings.redis.url),
        max_connections=settings.redis.max_connections,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_timeout_seconds,
        decode_responses=False,
    )
    _client = Redis(connection_pool=_pool)
    await _client.ping()
    logger.info(
        "Redis connected",
        extra={"max_connections": settings.redis.max_connections},
    )
    return _client


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
