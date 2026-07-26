"""Tests for cache key derivation and the caching decorator.

The tenant-scoping tests are the important ones: a key that omits the tenant
serves one customer's cached response to another, with a 200 status and nothing
in the logs to say so.
"""

from uuid import uuid7

from app.core.context import tenant_scope
from app.infrastructure.redis.cache import build_cache_key, cached


class TestCacheKeys:
    def test_the_same_arguments_give_the_same_key(self) -> None:
        assert build_cache_key("totals", 1, x=2) == build_cache_key("totals", 1, x=2)

    def test_different_arguments_give_different_keys(self) -> None:
        assert build_cache_key("totals", 1) != build_cache_key("totals", 2)

    def test_keyword_order_does_not_matter(self) -> None:
        assert build_cache_key("t", a=1, b=2) == build_cache_key("t", b=2, a=1)

    def test_the_prefix_is_readable(self) -> None:
        """So a human can tell what a key holds when debugging."""
        assert build_cache_key("invoice_totals", 1).startswith("invoice_totals:")

    def test_arguments_are_hashed_not_interpolated(self) -> None:
        """A value containing ':' must not be able to forge a different key."""
        assert "evil:injected" not in build_cache_key("t", "evil:injected")

    async def test_tenants_get_different_keys(self) -> None:
        """The leak this prevents: one tenant served another's cached response."""
        async with tenant_scope(uuid7()):
            first = build_cache_key("totals", 1)
        async with tenant_scope(uuid7()):
            second = build_cache_key("totals", 1)

        assert first != second

    async def test_the_same_tenant_gets_the_same_key(self) -> None:
        tenant_id = uuid7()
        async with tenant_scope(tenant_id):
            first = build_cache_key("totals", 1)
        async with tenant_scope(tenant_id):
            second = build_cache_key("totals", 1)

        assert first == second

    def test_opting_out_is_explicit(self) -> None:
        """Global values exist, but turning scoping off must be visible."""
        assert build_cache_key("flags", 1, tenant_scoped=False) != build_cache_key(
            "flags", 1
        )


class TestCachedDecorator:
    async def test_the_second_call_is_served_from_cache(self, fake_cache) -> None:
        calls: list[int] = []

        @cached("double", ttl_seconds=60)
        async def double(n: int) -> int:
            calls.append(n)
            return n * 2

        assert await double(21) == 42
        assert await double(21) == 42
        assert calls == [21]

    async def test_different_arguments_are_cached_separately(self, fake_cache) -> None:
        calls: list[int] = []

        @cached("double", ttl_seconds=60)
        async def double(n: int) -> int:
            calls.append(n)
            return n * 2

        await double(1)
        await double(2)
        assert calls == [1, 2]

    async def test_none_is_not_cached(self, fake_cache) -> None:
        """Distinguishing cached-None from a miss needs a sentinel; not worth it."""
        calls: list[int] = []

        @cached("nothing", ttl_seconds=60)
        async def nothing(n: int) -> None:
            calls.append(n)

        await nothing(1)
        await nothing(1)
        assert calls == [1, 1]

    async def test_the_wrapped_function_keeps_its_identity(self) -> None:
        @cached("x")
        async def documented(n: int) -> int:
            """A docstring worth preserving."""
            return n

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "A docstring worth preserving."

    async def test_tenants_do_not_share_cached_results(self, fake_cache) -> None:
        @cached("scoped", ttl_seconds=60)
        async def value() -> str:
            return str(uuid7())

        async with tenant_scope(uuid7()):
            first = await value()
        async with tenant_scope(uuid7()):
            second = await value()

        assert first != second
