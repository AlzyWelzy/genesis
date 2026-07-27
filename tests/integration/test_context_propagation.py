"""Ambient context surviving the jump into background work.

Why these exist
---------------
``request_scope``'s docstring says a job "carries the correlation ID in its
payload; restoring it here is what keeps the job's log lines attached to the
user action that caused it". ``stage()``'s says the relay "can restore the
context the event was produced in". Neither was implemented: nothing captured
the context at enqueue time and nothing rebound it at dispatch time.

The observability half of that is bad enough — every background log line was an
orphan. The tenancy half is worse and is a hard blocker for Stage 2: a
``TenantRepository`` reads the tenant from the ambient context, so a handler
doing tenant-scoped work inside a job did not merely lose its logging, it raised
``RuntimeError`` on ``require_tenant_id()``. Background work for a tenant was
impossible.

These tests assert the context is carried, is rebound, survives a retry, and —
critically — does not leak between concurrently running jobs.
"""

import asyncio
from uuid import UUID

import pytest

from app.core.context import (
    get_correlation_id,
    get_tenant_id,
    request_scope,
    require_tenant_id,
)
from app.infrastructure.queue.client import (
    DEAD_LETTER_KEY,
    DELAYED_KEY,
    STREAM_KEY,
    Job,
    RedisQueue,
    TaskRegistry,
)
from app.infrastructure.queue.worker import Worker
from app.infrastructure.redis.client import build_key, close_redis, init_redis

pytestmark = pytest.mark.integration

TENANT = UUID("aaaaaaaa-0000-7000-8000-00000000000c")
OTHER_TENANT = UUID("bbbbbbbb-0000-7000-8000-00000000000d")


@pytest.fixture
async def queue_redis():
    """A clean queue, skipping when Redis is unavailable."""
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    for key in (STREAM_KEY, DELAYED_KEY, DEAD_LETTER_KEY):
        await client.delete(build_key(key))

    yield client

    for key in (STREAM_KEY, DELAYED_KEY, DEAD_LETTER_KEY):
        await client.delete(build_key(key))
    await close_redis()


class TestEnqueueCapturesContext:
    async def test_the_context_is_written_onto_the_job(self, queue_redis) -> None:
        """Enqueue time is the last moment the context exists."""
        async with request_scope(correlation_id="corr-1", tenant_id=TENANT):
            fields = Job(name="t").encode("job-1")

        assert fields["correlation_id"] == "corr-1"
        assert fields["tenant_id"] == str(TENANT)

    async def test_absent_values_are_omitted_not_stringified(self, queue_redis) -> None:
        """``"None"`` as a field value is a decoding trap."""
        fields = Job(name="t").encode("job-1")

        assert "tenant_id" not in fields
        assert "correlation_id" not in fields


class TestWorkerRestoresContext:
    async def test_a_handler_sees_the_enqueuing_tenant(self, queue_redis) -> None:
        """The blocker for Stage 2: without this, ``require_tenant_id`` raises."""
        seen: list[UUID] = []

        registry = TaskRegistry()

        @registry.register("ctx.tenant")
        async def handler(_payload: dict) -> None:
            seen.append(require_tenant_id())

        async with request_scope(tenant_id=TENANT):
            await RedisQueue().enqueue(Job(name="ctx.tenant"))

        worker = Worker(registry, name="w-tenant", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert seen == [TENANT]

    async def test_a_handler_sees_the_correlation_id(self, queue_redis) -> None:
        """What keeps a job's log lines attached to the request that caused it."""
        seen: list[str | None] = []

        registry = TaskRegistry()

        @registry.register("ctx.corr")
        async def handler(_payload: dict) -> None:
            seen.append(get_correlation_id())

        async with request_scope(correlation_id="corr-xyz"):
            await RedisQueue().enqueue(Job(name="ctx.corr"))

        worker = Worker(registry, name="w-corr", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert seen == ["corr-xyz"]

    async def test_the_context_does_not_outlive_the_job(self, queue_redis) -> None:
        """A worker process is long-lived; a leaked tenant would poison it."""
        registry = TaskRegistry()

        @registry.register("ctx.leak")
        async def handler(_payload: dict) -> None:
            return

        async with request_scope(tenant_id=TENANT):
            await RedisQueue().enqueue(Job(name="ctx.leak"))

        worker = Worker(registry, name="w-leak", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        assert get_tenant_id() is None

    async def test_concurrent_jobs_do_not_see_each_others_tenant(
        self, queue_redis
    ) -> None:
        """Jobs run under ``asyncio.gather``; a shared binding would cross tenants.

        The most dangerous failure this could have: work performed for one
        customer against another customer's data, with nothing in the logs to
        show it.
        """
        observed: dict[str, UUID] = {}
        started = asyncio.Event()

        registry = TaskRegistry()

        @registry.register("ctx.concurrent")
        async def handler(payload: dict) -> None:
            label = payload["label"]
            if label == "first":
                # Hold this job open until the second has bound its own tenant,
                # so the two are genuinely interleaved rather than sequential.
                await started.wait()
            else:
                started.set()
                await asyncio.sleep(0)
            observed[label] = require_tenant_id()

        async with request_scope(tenant_id=TENANT):
            await RedisQueue().enqueue(
                Job(name="ctx.concurrent", payload={"label": "first"})
            )
        async with request_scope(tenant_id=OTHER_TENANT):
            await RedisQueue().enqueue(
                Job(name="ctx.concurrent", payload={"label": "second"})
            )

        worker = Worker(registry, name="w-concurrent", concurrency=2)
        await worker.setup()
        await worker._consume_batch()

        assert observed == {"first": TENANT, "second": OTHER_TENANT}


class TestRetryPreservesContext:
    async def test_a_retried_job_keeps_its_tenant(self, queue_redis) -> None:
        """Otherwise attempt 1 can do tenant work and attempt 2 cannot.

        The retry is re-serialised in the worker's failure path, outside the
        scope the handler ran under, so the fields have to be carried from the
        job rather than read from the ambient context.
        """
        registry = TaskRegistry()

        @registry.register("ctx.retry")
        async def handler(_payload: dict) -> None:
            raise ValueError("always fails")

        async with request_scope(correlation_id="corr-retry", tenant_id=TENANT):
            await RedisQueue().enqueue(Job(name="ctx.retry", max_retries=3))

        worker = Worker(registry, name="w-retry", concurrency=1)
        await worker.setup()
        await worker._consume_batch()

        delayed = await queue_redis.zrange(build_key(DELAYED_KEY), 0, -1)
        assert len(delayed) == 1

        import json

        entry = json.loads(delayed[0])
        assert entry["tenant_id"] == str(TENANT)
        assert entry["correlation_id"] == "corr-retry"
