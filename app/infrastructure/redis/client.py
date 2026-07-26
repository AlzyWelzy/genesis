"""Redis connection pool and client accessor.

Why this file exists
--------------------
Redis is used by three unrelated concerns — caching, pub/sub and queueing —
and each would otherwise build its own connection pool. This module owns the
single pool for the process so connection limits are predictable and shutdown
closes everything.

It exposes a client, nothing more. Semantics (what a key means, how long it
lives, what a channel carries) belong in :mod:`~app.infrastructure.redis.cache`
and :mod:`~app.infrastructure.redis.pubsub`.

Dependency note
---------------
``redis`` is not yet a project dependency. Imports are deliberately deferred
into the functions below so importing this module never breaks the app; add
``redis>=5`` to ``pyproject.toml`` before implementing.
"""

from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:
    # Unresolved until `redis` is added to pyproject dependencies. The import is
    # type-checking only, so nothing breaks at runtime in the meantime.
    from redis.asyncio import Redis  # ty: ignore[unresolved-import]

#: Lazily created, process-wide pool. Built in the lifespan, closed on shutdown.
_client: Redis[bytes] | None = None


async def init_redis() -> Redis[bytes]:
    """Create the connection pool and verify connectivity.

    Called once from the application lifespan. Pinging here turns a
    misconfigured Redis into a startup crash instead of a request-time failure.

    Returns:
        The connected client.
    """
    raise NotImplementedError(
        "Add `redis` to pyproject dependencies, then build the client here from "
        "settings.redis (url, max_connections, socket_timeout_seconds) and "
        "await client.ping()."
    )


def get_redis() -> Redis[bytes]:
    """Return the initialised client.

    Raises:
        RuntimeError: When called before the lifespan initialised the pool —
            a programming error worth failing loudly on.
    """
    if _client is None:
        raise RuntimeError("Redis client is not initialised; call init_redis() first.")
    return _client


async def close_redis() -> None:
    """Close the pool. Called from the application lifespan on shutdown."""
    # TODO: await _client.aclose() and reset the module-level reference.


def build_key(*parts: Any) -> str:
    """Join key parts under the configured namespace.

    Every key written through this package must go through here. A shared Redis
    instance without a namespace is how a staging deploy silently reads
    production cache entries.

    Args:
        *parts: Key segments, joined with ``:``.

    Returns:
        The fully namespaced key.
    """
    return ":".join((settings.redis.key_prefix, *(str(p) for p in parts)))
