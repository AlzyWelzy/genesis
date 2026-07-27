"""The durable event path, end to end.

Why these exist
---------------
``publish_after_commit(..., durable=True)`` is the mode the docs recommend "for
anything whose loss a customer would notice — payments, receipts, provisioning".
It wrote rows to ``outbox_messages`` that **nothing ever published**:
``OutboxRelay`` was defined, exported and documented, and never instantiated in
any process. The table simply grew.

That is the outbox's own failure mode turned inside out. The pattern exists to
convert "silently lost" into "at least once", and with no relay running it
delivered "silently lost" with an audit trail.

These tests drive the real chain — stage inside a transaction, claim, publish,
mark published — against real PostgreSQL and Redis, and cover the failure paths
that decide whether an event survives a bad day: a publisher that raises must
leave the row unpublished and retryable, and a crash after publishing must not
double-deliver.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar
from uuid import UUID

import pytest
from sqlalchemy import select

from app.core.context import request_scope
from app.events.base import DomainEvent
from app.events.bus import publish_after_commit
from app.infrastructure.database.session import session_scope
from app.infrastructure.outbox.models import OutboxMessage
from app.infrastructure.outbox.relay import (
    MAX_ATTEMPTS,
    OutboxRelay,
    outbox_task_name,
    pending_count,
    publish_to_queue,
    purge_published,
)
from app.infrastructure.queue.client import STREAM_KEY, InMemoryQueue, set_queue
from app.infrastructure.redis.client import build_key, close_redis, init_redis

pytestmark = pytest.mark.integration

TENANT = UUID("aaaaaaaa-0000-7000-8000-00000000000e")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoicePaid(DomainEvent):
    """A durable test event."""

    name: ClassVar[str] = "test.invoice_paid"

    invoice_id: str


@pytest.fixture
async def clean_outbox(shared_engine):
    """Start from an empty outbox, and leave one behind.

    Uses its own committed transactions rather than the rollback-per-test
    ``session`` fixture: the relay opens its own ``session_scope``, so a row
    staged inside an uncommitted transaction is invisible to it — which is the
    whole point of the pattern being tested. ``shared_engine`` is what makes the
    module-level engine usable from this test's event loop.
    """
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(delete(OutboxMessage))

    yield

    async with session_scope() as session:
        await session.execute(delete(OutboxMessage))


async def _stage(event: DomainEvent, *, tenant: UUID | None = TENANT) -> None:
    """Stage an event in its own committed transaction."""
    async with (
        request_scope(tenant_id=tenant, correlation_id="corr-outbox"),
        session_scope() as session,
    ):
        await publish_after_commit(session, event, durable=True)


async def _rows() -> list[OutboxMessage]:
    async with session_scope() as session:
        result = await session.execute(
            select(OutboxMessage).order_by(OutboxMessage.created_at)
        )
        return list(result.scalars().all())


class TestRelayDelivery:
    async def test_a_staged_event_is_published(self, clean_outbox) -> None:
        """The bug this file exists for: no process ever ran a relay."""
        published: list[OutboxMessage] = []

        async def publisher(message: OutboxMessage) -> None:
            published.append(message)

        await _stage(InvoicePaid(invoice_id="inv-1"))
        count = await OutboxRelay(publisher).run_once()

        assert count == 1
        assert published[0].name == "test.invoice_paid"
        assert published[0].payload["payload"]["invoice_id"] == "inv-1"

    async def test_a_published_row_is_marked_and_not_republished(
        self, clean_outbox
    ) -> None:
        """At-least-once must not become every-pass."""
        seen = 0

        async def publisher(_message: OutboxMessage) -> None:
            nonlocal seen
            seen += 1

        await _stage(InvoicePaid(invoice_id="inv-1"))
        relay = OutboxRelay(publisher)

        assert await relay.run_once() == 1
        assert await relay.run_once() == 0
        assert seen == 1

        [row] = await _rows()
        assert row.published_at is not None

    async def test_the_tenant_and_correlation_are_carried(self, clean_outbox) -> None:
        """What lets the relay restore the context the event was produced in."""
        await _stage(InvoicePaid(invoice_id="inv-1"))

        [row] = await _rows()
        assert row.tenant_id == TENANT
        assert row.correlation_id == "corr-outbox"

    async def test_the_publisher_runs_under_the_staged_context(
        self, clean_outbox
    ) -> None:
        """Otherwise the trail from request to effect breaks at the relay."""
        from app.core.context import get_correlation_id, get_tenant_id

        observed: list[tuple[UUID | None, str | None]] = []

        async def publisher(_message: OutboxMessage) -> None:
            observed.append((get_tenant_id(), get_correlation_id()))

        await _stage(InvoicePaid(invoice_id="inv-1"))
        await OutboxRelay(publisher).run_once()

        assert observed == [(TENANT, "corr-outbox")]


class TestFailureHandling:
    async def test_a_failed_publish_leaves_the_row_unpublished(
        self, clean_outbox
    ) -> None:
        """Marking it published on failure would lose the event outright."""

        async def broken(_message: OutboxMessage) -> None:
            raise RuntimeError("downstream is down")

        await _stage(InvoicePaid(invoice_id="inv-1"))
        assert await OutboxRelay(broken).run_once() == 0

        [row] = await _rows()
        assert row.published_at is None
        assert row.attempts == 1
        assert "downstream is down" in (row.last_error or "")

    async def test_a_failure_schedules_a_backed_off_retry(self, clean_outbox) -> None:
        """Without backoff the retries become the load keeping it down."""

        async def broken(_message: OutboxMessage) -> None:
            raise RuntimeError("nope")

        await _stage(InvoicePaid(invoice_id="inv-1"))
        await OutboxRelay(broken).run_once()

        [row] = await _rows()
        assert row.next_attempt_at is not None

    async def test_one_bad_message_does_not_block_the_batch(self, clean_outbox) -> None:
        """A poison message must not stop everything queued behind it."""
        delivered: list[str] = []

        async def selective(message: OutboxMessage) -> None:
            invoice = message.payload["payload"]["invoice_id"]
            if invoice == "poison":
                raise RuntimeError("cannot publish")
            delivered.append(invoice)

        await _stage(InvoicePaid(invoice_id="poison"))
        await _stage(InvoicePaid(invoice_id="good"))

        assert await OutboxRelay(selective).run_once() == 1
        assert delivered == ["good"]

    async def test_an_exhausted_message_is_kept_not_deleted(self, clean_outbox) -> None:
        """The payload is frequently the only record of what should have happened."""

        async def broken(_message: OutboxMessage) -> None:
            raise RuntimeError("nope")

        await _stage(InvoicePaid(invoice_id="inv-1"))
        relay = OutboxRelay(broken)

        for _ in range(MAX_ATTEMPTS + 2):
            async with session_scope() as session:
                # Clear the backoff so the next pass claims it again.
                for row in (await session.execute(select(OutboxMessage))).scalars():
                    row.next_attempt_at = None
            await relay.run_once()

        [row] = await _rows()
        assert row.published_at is None
        assert row.attempts == MAX_ATTEMPTS


class TestQueueBridge:
    """The default publisher: outbox → queue, so retries and DLQ come free."""

    @pytest.fixture
    async def redis_queue(self, clean_outbox):
        try:
            client = await init_redis()
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
            pytest.skip(f"Redis unavailable: {exc}")
        await client.delete(build_key(STREAM_KEY))
        yield client
        await client.delete(build_key(STREAM_KEY))
        await close_redis()

    async def test_the_event_is_enqueued_under_a_namespaced_task_name(
        self, redis_queue
    ) -> None:
        fake = InMemoryQueue()
        set_queue(fake)

        await _stage(InvoicePaid(invoice_id="inv-1"))
        assert await OutboxRelay(publish_to_queue).run_once() == 1

        assert [job.name for job in fake.jobs] == [
            outbox_task_name("test.invoice_paid")
        ]

    async def test_the_job_carries_an_idempotency_key(self, redis_queue) -> None:
        """A crash between publish and mark-published republishes the row.

        At-least-once is the guarantee; the key is what stops it becoming
        at-least-twice for the handler.
        """
        fake = InMemoryQueue()
        set_queue(fake)

        await _stage(InvoicePaid(invoice_id="inv-1"))
        await OutboxRelay(publish_to_queue).run_once()

        [row] = await _rows()
        assert fake.jobs[0].idempotency_key == f"outbox:{row.id}"


class TestHousekeeping:
    async def test_pending_count_reports_unpublished_rows(self, clean_outbox) -> None:
        """The single most useful outbox metric; alert on it climbing."""
        await _stage(InvoicePaid(invoice_id="inv-1"))
        assert await pending_count() == 1

        async def publisher(_message: OutboxMessage) -> None:
            return

        await OutboxRelay(publisher).run_once()
        assert await pending_count() == 0

    async def test_purge_removes_only_published_rows(self, clean_outbox) -> None:
        """Deleting an unpublished row is silent data loss."""

        async def publisher(_message: OutboxMessage) -> None:
            return

        await _stage(InvoicePaid(invoice_id="published"))
        await OutboxRelay(publisher).run_once()
        await _stage(InvoicePaid(invoice_id="pending"))

        deleted = await purge_published(timedelta(seconds=-1))

        assert deleted == 1
        remaining = [r.payload["payload"]["invoice_id"] for r in await _rows()]
        assert remaining == ["pending"]
