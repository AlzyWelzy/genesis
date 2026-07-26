"""Cache abstraction.

Why this file exists
--------------------
Services must be able to say "cache this for five minutes" without knowing that
the backing store is Redis, that values are JSON-encoded, or that keys carry an
environment prefix. Expressing the cache as a protocol gives two concrete
benefits: tests swap in an in-memory implementation with no Redis running, and
a future move to a different store touches one file.

The interface is intentionally minimal — get, set, delete, invalidate by
pattern. Anything richer (tag-based invalidation, read-through wrappers) can be
built on top without widening the contract every implementation must satisfy.

Caching rules worth stating once
--------------------------------
* A cache miss must never be an error; degrade to the source of truth.
* A Redis outage must not take the API down — swallow connection errors on read.
* Never cache a value the caller is not allowed to see; scope keys by tenant.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """Key-value cache with expiry.

    Implementations are expected to be safe to share across tasks.
    """

    async def get(self, key: str) -> Any | None:
        """Return the cached value, or ``None`` on miss or decode failure."""
        ...

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store a value, defaulting to the configured TTL when none is given."""
        ...

    async def delete(self, *keys: str) -> None:
        """Remove keys. Missing keys are not an error."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether a key is present and unexpired."""
        ...

    async def clear_prefix(self, prefix: str) -> None:
        """Remove every key under a prefix, for coarse invalidation."""
        ...


class RedisCache:
    """Redis-backed :class:`Cache` implementation.

    Values are serialised to JSON, so only JSON-representable objects may be
    cached. Storing pickles would allow arbitrary code execution if the store
    were ever compromised.
    """

    async def get(self, key: str) -> Any | None:
        """Read and decode a value."""
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Encode and write a value with an expiry."""
        raise NotImplementedError

    async def delete(self, *keys: str) -> None:
        """Delete one or more keys."""
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        """Check key presence."""
        raise NotImplementedError

    async def clear_prefix(self, prefix: str) -> None:
        """Invalidate a namespace.

        Must use ``SCAN``, never ``KEYS`` — ``KEYS`` blocks the Redis event loop
        for the entire keyspace and will stall production.
        """
        raise NotImplementedError


class InMemoryCache:
    """Dict-backed :class:`Cache` for tests and local development.

    Not shared between processes and not bounded — never use in production.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        """Return a stored value, ignoring expiry."""
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store a value; TTL handling is left to the implementation."""
        raise NotImplementedError

    async def delete(self, *keys: str) -> None:
        """Discard keys."""
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        """Check key presence."""
        raise NotImplementedError

    async def clear_prefix(self, prefix: str) -> None:
        """Discard every key with the given prefix."""
        raise NotImplementedError


# TODO: add a `cached()` decorator building keys from the function signature,
# once the key-derivation rules (tenant scoping, argument hashing) are settled.
# TODO: add single-flight / stampede protection for expensive recomputations.
