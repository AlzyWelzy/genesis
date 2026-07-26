"""Cache abstraction.

Why this file exists
--------------------
Services must be able to say "cache this for five minutes" without knowing the
backing store is Redis, that values are JSON-encoded, or that keys carry an
environment prefix. Expressing the cache as a protocol gives two concrete
benefits: tests swap in an in-memory implementation with no Redis running, and
a future move to a different store touches one file.

Caching rules worth stating once
--------------------------------
* **A cache miss is never an error.** Degrade to the source of truth.
* **A Redis outage must not take the API down.** Read failures are swallowed
  and logged; the caller sees a miss, which it already knows how to handle.
* **Never cache a value the caller is not allowed to see.** Keys must be scoped
  by tenant, or one customer's cached response is served to another.
* **JSON, never pickle.** Unpickling attacker-controlled bytes is arbitrary code
  execution, and a compromised cache should not become a compromised app.
"""

import json
import time
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)

#: Scan batch size for prefix invalidation. Large enough to keep round trips
#: down, small enough that each call returns promptly.
_SCAN_BATCH = 500


@runtime_checkable
class Cache(Protocol):
    """Key-value cache with expiry.

    Implementations must be safe to share across concurrent tasks.
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
    cached.

    Every read failure is swallowed and logged as a miss. That is the whole
    point of a cache layer: an unavailable cache should make the application
    slower, never broken. Write failures are swallowed for the same reason.
    """

    async def get(self, key: str) -> Any | None:
        """Read and decode a value, treating any failure as a miss."""
        try:
            raw = await get_redis().get(build_key(key))
        except Exception:  # noqa: BLE001 - a cache outage must not break reads
            logger.warning("Cache read failed", extra={"cache_key": key})
            return None

        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A value written by an older schema. Treat as a miss rather than
            # raising; the caller recomputes and overwrites it.
            logger.warning("Cache value could not be decoded", extra={"cache_key": key})
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Encode and write a value with an expiry.

        A TTL is always applied. An entry without one lives until it is evicted
        under memory pressure, which makes stale data unbounded in time and the
        cache impossible to reason about.
        """
        ttl = ttl_seconds if ttl_seconds is not None else settings.redis.default_ttl_seconds
        try:
            await get_redis().set(build_key(key), json.dumps(value), ex=ttl)
        except (TypeError, ValueError):
            # Not serialisable: a programming error, and worth surfacing rather
            # than silently not caching.
            raise
        except Exception:  # noqa: BLE001 - a cache outage must not break writes
            logger.warning("Cache write failed", extra={"cache_key": key})

    async def delete(self, *keys: str) -> None:
        """Delete one or more keys."""
        if not keys:
            return
        try:
            await get_redis().delete(*(build_key(key) for key in keys))
        except Exception:  # noqa: BLE001 - best-effort invalidation
            logger.warning("Cache delete failed", extra={"cache_keys": list(keys)})

    async def exists(self, key: str) -> bool:
        """Check key presence."""
        try:
            return bool(await get_redis().exists(build_key(key)))
        except Exception:  # noqa: BLE001 - unknown means "not cached"
            return False

    async def clear_prefix(self, prefix: str) -> None:
        """Invalidate a namespace.

        Uses ``SCAN``, never ``KEYS``. ``KEYS`` walks the entire keyspace in one
        blocking operation and will stall a production Redis for every other
        client while it runs.
        """
        pattern = f"{build_key(prefix)}*"
        try:
            client = get_redis()
            batch: list[bytes] = []
            async for found in client.scan_iter(match=pattern, count=_SCAN_BATCH):
                batch.append(found)
                if len(batch) >= _SCAN_BATCH:
                    await client.delete(*batch)
                    batch.clear()
            if batch:
                await client.delete(*batch)
        except Exception:  # noqa: BLE001 - best-effort invalidation
            logger.warning("Cache prefix clear failed", extra={"prefix": prefix})


class InMemoryCache:
    """Dict-backed :class:`Cache` for tests and local development.

    Not shared between processes and not bounded — never use in production.
    Expiry is checked lazily on read rather than by a background sweep, which
    is sufficient for a cache whose lifetime is one test.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float | None]] = {}

    def _is_expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= time.monotonic()

    async def get(self, key: str) -> Any | None:
        """Return a stored value, honouring its expiry."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._is_expired(expires_at):
            del self._store[key]
            return None
        # Round-trip through JSON so this implementation has the same
        # constraints as the Redis one: a test must not pass here because it
        # cached an object Redis could never have stored.
        return json.loads(json.dumps(value))

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store a value with an expiry."""
        ttl = ttl_seconds if ttl_seconds is not None else settings.redis.default_ttl_seconds
        json.dumps(value)  # fail fast on unserialisable values, as Redis would
        self._store[key] = (value, time.monotonic() + ttl if ttl else None)

    async def delete(self, *keys: str) -> None:
        """Discard keys."""
        for key in keys:
            self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        """Check key presence."""
        return await self.get(key) is not None

    async def clear_prefix(self, prefix: str) -> None:
        """Discard every key with the given prefix."""
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]

    def clear(self) -> None:
        """Discard everything. For test teardown."""
        self._store.clear()


#: Process-wide cache. Replaced with :class:`InMemoryCache` in tests.
cache: Cache = RedisCache()


def set_cache(implementation: Cache) -> None:
    """Replace the process-wide cache.

    For tests, which install an :class:`InMemoryCache` so no Redis is needed.
    """
    global cache  # noqa: PLW0603 - process-wide singleton by design
    cache = implementation


# TODO: add a `cached()` decorator building keys from the function signature,
# once the key-derivation rules (tenant scoping, argument hashing) are settled.
# TODO: add single-flight protection so a cold key under load triggers one
# recomputation rather than one per concurrent caller.
