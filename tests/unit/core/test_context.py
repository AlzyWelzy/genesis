"""Tests for request-scoped ambient context.

The isolation test is the important one. The entire justification for using
``ContextVar`` over a module global is that concurrent tasks must not see each
other's values — and in a multi-tenant system, a leak between two in-flight
requests is a cross-tenant data leak.
"""

import asyncio
import uuid

import pytest

from app.core.context import (
    current_context,
    request_id_var,
    require_tenant_id,
    tenant_id_var,
)


class TestTenantScope:
    """The guard that prevents unscoped tenant queries."""

    def test_require_tenant_id_raises_when_unset(self) -> None:
        """Returning None here would mean a query silently runs unscoped."""
        tenant_id_var.set(None)
        with pytest.raises(RuntimeError, match="No tenant in context"):
            require_tenant_id()

    def test_require_tenant_id_returns_the_bound_tenant(self) -> None:
        tenant_id = uuid.uuid7()
        tenant_id_var.set(tenant_id)
        try:
            assert require_tenant_id() == tenant_id
        finally:
            tenant_id_var.set(None)


class TestIsolation:
    """Concurrent tasks must not observe each other's context."""

    async def test_concurrent_tasks_keep_separate_tenants(self) -> None:
        observed: list[uuid.UUID] = []

        async def handle(tenant_id: uuid.UUID, delay: float) -> None:
            tenant_id_var.set(tenant_id)
            # Yield control so the tasks interleave — a shared global would be
            # overwritten by whichever task ran last before this resumed.
            await asyncio.sleep(delay)
            observed.append(require_tenant_id())

        first, second = uuid.uuid7(), uuid.uuid7()
        await asyncio.gather(handle(first, 0.02), handle(second, 0.01))

        assert set(observed) == {first, second}

    async def test_context_does_not_leak_out_of_a_task(self) -> None:
        """A value set inside a task must not escape into its parent."""
        tenant_id_var.set(None)

        async def inner() -> None:
            tenant_id_var.set(uuid.uuid7())

        await asyncio.create_task(inner())
        assert tenant_id_var.get() is None


class TestSnapshot:
    """Capturing context for somewhere it does not propagate."""

    def test_current_context_captures_all_values(self) -> None:
        tenant_id = uuid.uuid7()
        request_id_var.set("req-1")
        tenant_id_var.set(tenant_id)
        try:
            snapshot = current_context()
            assert snapshot.request_id == "req-1"
            assert snapshot.tenant_id == tenant_id
        finally:
            request_id_var.set(None)
            tenant_id_var.set(None)
