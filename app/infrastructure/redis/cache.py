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

import asyncio
import contextlib
import functools
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.core.context import get_tenant_id
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)


def _resolve_ttl(ttl_seconds: int | None) -> int:
    """Return the effective TTL, falling back to the configured default."""
    return (
        ttl_seconds if ttl_seconds is not None else settings.redis.default_ttl_seconds
    )


#: Scan batch size for prefix invalidation. Large enough to keep round trips
#: down, small enough that each call returns promptly.
_SCAN_BATCH = 500

#: How long a stampede loser waits before re-reading. Long enough for a fast
#: computation to land, short enough not to add noticeable latency.
_STAMPEDE_WAIT_SECONDS = 0.05


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
        except json.JSONDecodeError, UnicodeDecodeError:
            # A value written by an older schema. Treat as a miss rather than
            # raising; the caller recomputes and overwrites it.
            logger.warning("Cache value could not be decoded", extra={"cache_key": key})
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Encode and write a value with an expiry.

        A TTL is always applied. An entry without one lives until it is evicted
        under memory pressure, which makes stale data unbounded in time and the
        cache impossible to reason about.

        A non-positive TTL means "already expired", so nothing is stored.
        Redis rejects ``EX 0`` outright, so passing it through would turn an
        edge case into an exception on a path that must never raise.
        """
        ttl = _resolve_ttl(ttl_seconds)
        if ttl <= 0:
            await self.delete(key)
            return
        try:
            await get_redis().set(build_key(key), json.dumps(value), ex=ttl)
        except TypeError, ValueError:
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
        """Store a value with an expiry.

        Mirrors :class:`RedisCache` exactly, including the non-positive-TTL
        behaviour: a fake that accepts what the real implementation rejects is
        how a test passes locally and the same code fails in production.
        """
        json.dumps(value)  # fail fast on unserialisable values, as Redis would
        ttl = _resolve_ttl(ttl_seconds)
        if ttl <= 0:
            self._store.pop(key, None)
            return
        self._store[key] = (value, time.monotonic() + ttl)

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


def get_cache() -> Cache:
    """Return the current process-wide cache.

    Reach the cache through this rather than ``from ... import cache``. A
    ``from``-import binds the object into the importing module at import time,
    so a later :func:`set_cache` rebinds only this module's name and leaves the
    importer holding the previous instance — in tests, the in-memory fake is
    installed and the real Redis cache is used anyway.

    Matches ``get_queue``, ``get_redis``, ``get_storage``, ``get_email_provider``
    and ``get_metrics``, which exist for the same reason.
    """
    return cache


def set_cache(implementation: Cache) -> None:
    """Replace the process-wide cache.

    For tests, which install an :class:`InMemoryCache` so no Redis is needed.
    """
    global cache  # noqa: PLW0603 - process-wide singleton by design
    cache = implementation


def build_cache_key(
    prefix: str, *args: Any, tenant_scoped: bool = True, **kwargs: Any
) -> str:
    """Derive a cache key from a prefix and the arguments that identify a value.

    **Tenant scoping is on by default, and that default is load-bearing.** A key
    that omits the tenant serves one customer's cached response to another —
    a cross-tenant leak with no error, no log line and a 200 status. Turn it off
    only for genuinely global values (feature flags, system configuration), and
    say so at the call site.

    Arguments are hashed rather than interpolated, so a value containing ``:``
    cannot forge a different key, and a long argument list cannot produce a key
    too large to store.

    Args:
        prefix: Namespace for this kind of value, e.g. ``"invoice_totals"``.
        *args: Positional arguments identifying the value.
        tenant_scoped: Include the current tenant. Leave on unless the value is
            genuinely shared between tenants.
        **kwargs: Keyword arguments identifying the value.

    Returns:
        The cache key, without the global Redis namespace, which
        :func:`~app.infrastructure.redis.client.build_key` adds.
    """
    parts: list[str] = [prefix]
    if tenant_scoped:
        # `get_tenant_id`, not `require_tenant_id`: caching outside a tenant
        # scope is legitimate for a background job, and the "global" bucket it
        # falls into is still separate from any tenant's.
        parts.append(str(get_tenant_id() or "global"))

    signature = json.dumps([args, sorted(kwargs.items())], sort_keys=True, default=str)
    parts.append(hashlib.sha256(signature.encode()).hexdigest()[:32])
    return ":".join(parts)


def cached(
    prefix: str,
    *,
    ttl_seconds: int | None = None,
    tenant_scoped: bool = True,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Cache an async function's result, keyed by its arguments.

    Usage::

        @cached("invoice_totals", ttl_seconds=300)
        async def compute_totals(invoice_id: UUID) -> dict: ...

    A miss, a decode failure or an unreachable Redis all fall through to the
    wrapped function. The cache can only ever make this faster, never wrong —
    which is the property that lets it be added to an existing function without
    changing its contract.

    ``None`` returns are **not** cached. Distinguishing "cached None" from
    "cache miss" needs a sentinel, and the ambiguity is rarely worth it for a
    value that is usually cheap to recompute.

    Args:
        prefix: Namespace for this function's cached values.
        ttl_seconds: Lifetime, defaulting to the configured TTL.
        tenant_scoped: Include the current tenant in the key. See
            :func:`build_cache_key` for why this defaults to ``True``.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = build_cache_key(prefix, *args, tenant_scoped=tenant_scoped, **kwargs)
            if (hit := await cache.get(key)) is not None:
                return hit

            value = await func(*args, **kwargs)
            if value is not None:
                await cache.set(key, value, ttl_seconds)
            return value

        return wrapper

    return decorator


async def get_or_compute(
    key: str,
    compute: Callable[[], Awaitable[Any]],
    *,
    ttl_seconds: int | None = None,
    lock_timeout_seconds: int = 10,
) -> Any:
    """Read a cached value, computing it once across all callers on a miss.

    Solves the **cache stampede**. When a popular key expires under load, every
    concurrent request misses simultaneously and every one of them runs the
    expensive computation — so the moment the cache is least able to help is
    the moment the database receives the most traffic. A key recomputed in two
    seconds and requested a thousand times a second causes two thousand
    identical queries.

    One caller wins a short Redis lock and computes; the others wait briefly and
    re-read. A loser that finds nothing after waiting computes anyway rather
    than failing — a duplicated computation is better than a dropped request.

    The lock carries a timeout, so a worker that dies mid-computation cannot
    block every other caller indefinitely.

    Args:
        key: Cache key, from :func:`build_cache_key`.
        compute: Produces the value on a miss.
        ttl_seconds: Lifetime for the cached value.
        lock_timeout_seconds: Ceiling on how long the lock is held.

    Returns:
        The cached or freshly computed value.
    """
    if (hit := await cache.get(key)) is not None:
        return hit

    lock_key = build_key("lock", key)
    try:
        acquired = await get_redis().set(
            lock_key, "1", nx=True, ex=lock_timeout_seconds
        )
    except Exception:  # noqa: BLE001 - a lock outage must not fail the request
        acquired = True

    if not acquired:
        # Someone else is computing. Wait briefly, then re-read.
        await asyncio.sleep(_STAMPEDE_WAIT_SECONDS)
        if (hit := await cache.get(key)) is not None:
            return hit
        # Still nothing: the winner may have died. Compute rather than fail.

    try:
        value = await compute()
        if value is not None:
            await cache.set(key, value, ttl_seconds)
        return value
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await get_redis().delete(lock_key)
