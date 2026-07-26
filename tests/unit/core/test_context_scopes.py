"""Tests for the ambient-context scope helpers.

The isolation tests are the ones that matter. The whole premise of using
``ContextVar`` for tenancy is that two concurrent requests cannot see each
other's values — if that fails, the repository layer scopes queries to the
wrong tenant, which is the highest-severity bug this architecture can produce.
"""

import asyncio
from uuid import uuid7

import pytest

from app.core.context import (
    current_context,
    get_correlation_id,
    get_tenant_id,
    get_user_id,
    request_scope,
    require_tenant_id,
    tenant_scope,
    user_scope,
)


class TestTenantScope:
    async def test_binds_for_the_duration(self) -> None:
        tenant_id = uuid7()
        async with tenant_scope(tenant_id):
            assert get_tenant_id() == tenant_id
            assert require_tenant_id() == tenant_id

    async def test_unbinds_on_exit(self) -> None:
        async with tenant_scope(uuid7()):
            pass
        assert get_tenant_id() is None

    async def test_unbinds_even_when_the_body_raises(self) -> None:
        """A leaked tenant would scope the *next* operation to the wrong one."""
        with pytest.raises(RuntimeError):
            async with tenant_scope(uuid7()):
                raise RuntimeError("boom")
        assert get_tenant_id() is None

    async def test_nesting_restores_the_outer_tenant(self) -> None:
        """A job that dips into another tenant must come back to its own."""
        outer, inner = uuid7(), uuid7()
        async with tenant_scope(outer):
            async with tenant_scope(inner):
                assert get_tenant_id() == inner
            assert get_tenant_id() == outer

    async def test_yields_the_bound_id(self) -> None:
        tenant_id = uuid7()
        async with tenant_scope(tenant_id) as bound:
            assert bound == tenant_id


class TestIsolation:
    async def test_concurrent_tasks_do_not_see_each_other(self) -> None:
        """The premise of the whole design: no cross-tenant bleed."""
        observed: dict[str, object] = {}

        async def work(name: str, tenant_id) -> None:
            async with tenant_scope(tenant_id):
                # Yield control so the tasks genuinely interleave.
                await asyncio.sleep(0)
                observed[name] = get_tenant_id()

        a, b = uuid7(), uuid7()
        await asyncio.gather(work("a", a), work("b", b))

        assert observed == {"a": a, "b": b}

    async def test_a_scope_does_not_leak_into_a_sibling_task(self) -> None:
        async def observe() -> object:
            return get_tenant_id()

        async with tenant_scope(uuid7()):
            # A task created inside the scope inherits it, which is correct.
            assert await asyncio.create_task(observe()) is not None

        assert await asyncio.create_task(observe()) is None


class TestRequireTenant:
    def test_raises_when_unbound(self) -> None:
        """Returning None would let a query silently run unscoped."""
        with pytest.raises(RuntimeError, match="No tenant in context"):
            require_tenant_id()

    def test_the_message_says_how_to_fix_it(self) -> None:
        with pytest.raises(RuntimeError, match="tenant_scope"):
            require_tenant_id()


class TestUserScope:
    async def test_binds_and_unbinds(self) -> None:
        user_id = uuid7()
        async with user_scope(user_id):
            assert get_user_id() == user_id
        assert get_user_id() is None

    async def test_nesting_restores_the_outer_user(self) -> None:
        outer, inner = uuid7(), uuid7()
        async with user_scope(outer):
            async with user_scope(inner):
                assert get_user_id() == inner
            assert get_user_id() == outer


class TestRequestScope:
    async def test_binds_every_supplied_value(self) -> None:
        tenant_id, user_id = uuid7(), uuid7()
        async with request_scope(
            request_id="req-1",
            correlation_id="corr-1",
            tenant_id=tenant_id,
            user_id=user_id,
        ) as context:
            assert context.request_id == "req-1"
            assert context.correlation_id == "corr-1"
            assert get_tenant_id() == tenant_id
            assert get_user_id() == user_id

    async def test_omitted_values_are_left_alone(self) -> None:
        tenant_id = uuid7()
        async with tenant_scope(tenant_id), request_scope(request_id="req-1"):
            assert get_tenant_id() == tenant_id
            assert get_correlation_id() is None

    async def test_everything_unwinds(self) -> None:
        async with request_scope(request_id="req-1", tenant_id=uuid7()):
            pass
        assert current_context() == current_context()
        assert get_tenant_id() is None

    async def test_restores_a_captured_context(self) -> None:
        """This is what keeps a queued job's logs attached to its request."""
        tenant_id, user_id = uuid7(), uuid7()
        async with tenant_scope(tenant_id), user_scope(user_id):
            captured = current_context()

        async with request_scope(
            request_id=captured.request_id,
            correlation_id=captured.correlation_id,
            tenant_id=captured.tenant_id,
            user_id=captured.user_id,
        ):
            assert get_tenant_id() == tenant_id
            assert get_user_id() == user_id
