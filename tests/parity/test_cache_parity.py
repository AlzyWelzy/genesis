"""``InMemoryCache`` must accept and reject exactly what ``RedisCache`` does.

The fake round-trips every value through JSON deliberately, so a value Redis
could not store fails identically in a unit test. These tests hold that property
rather than trusting the comment that asserts it.
"""

import datetime as dt
from decimal import Decimal
from uuid import uuid7

import pytest

from app.infrastructure.redis.cache import InMemoryCache, RedisCache
from app.infrastructure.redis.client import close_redis, init_redis

pytestmark = pytest.mark.integration

#: Values a caller might plausibly try to cache. The unserialisable ones matter
#: most: a fake that accepted them would let a test pass on a write Redis
#: rejects.
CACHEABLE = [
    ("dict", {"a": 1}),
    ("list", [1, 2]),
    ("string", "s"),
    ("integer", 7),
    ("float", 1.5),
    ("boolean", True),
    ("none", None),
    ("nested", {"x": [{"y": 1}]}),
]
UNCACHEABLE = [
    ("uuid", uuid7()),
    ("datetime", dt.datetime.now(dt.UTC)),
    ("decimal", Decimal("1.5")),
    ("set", {1, 2}),
    ("bytes", b"ab"),
]


@pytest.fixture
async def implementations():
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    fake, real = InMemoryCache(), RedisCache()
    yield fake, real
    fake.clear()
    await close_redis()


class TestValueAcceptance:
    @pytest.mark.parametrize(("label", "value"), CACHEABLE)
    async def test_both_store_and_return_the_same_thing(
        self, implementations, label: str, value: object
    ) -> None:
        fake, real = implementations
        key = f"parity:{label}"

        for cache in (fake, real):
            await cache.set(key, value, ttl_seconds=30)

        assert await fake.get(key) == await real.get(key)

    @pytest.mark.parametrize(("label", "value"), UNCACHEABLE)
    async def test_both_reject_what_redis_cannot_store(
        self, implementations, label: str, value: object
    ) -> None:
        """A fake that accepted these would hide the failure until production.

        The value is not JSON, so Redis cannot hold it. The fake round-trips
        through JSON for exactly this reason.
        """
        fake, real = implementations

        for cache in (fake, real):
            with pytest.raises(TypeError):
                await cache.set(f"parity:{label}", value, ttl_seconds=30)

    async def test_a_tuple_comes_back_as_a_list_from_both(
        self, implementations
    ) -> None:
        """JSON has no tuple. Silently changing type is worth pinning."""
        fake, real = implementations

        for cache in (fake, real):
            await cache.set("parity:tuple", (1, 2), ttl_seconds=30)

        from_fake = await fake.get("parity:tuple")
        from_real = await real.get("parity:tuple")

        assert from_fake == from_real == [1, 2]


class TestMissAndExpiry:
    async def test_a_missing_key_is_none_in_both(self, implementations) -> None:
        fake, real = implementations

        assert await fake.get("parity:absent") is None
        assert await real.get("parity:absent") is None

    @pytest.mark.parametrize("ttl", [0, -1])
    async def test_a_non_positive_ttl_stores_nothing_in_either(
        self, implementations, ttl: int
    ) -> None:
        """Redis rejects ``EX 0``, so the fake must not treat it as "forever"."""
        fake, real = implementations

        for cache in (fake, real):
            await cache.set("parity:ttl", "v", ttl_seconds=ttl)

        assert await fake.get("parity:ttl") is None
        assert await real.get("parity:ttl") is None

    async def test_delete_is_idempotent_in_both(self, implementations) -> None:
        fake, real = implementations

        for cache in (fake, real):
            await cache.set("parity:del", "v", ttl_seconds=30)
            await cache.delete("parity:del")
            await cache.delete("parity:del")

        assert await fake.get("parity:del") is None
        assert await real.get("parity:del") is None
