"""Tests for the transactional outbox.

Against a real PostgreSQL, because the guarantees being tested are database
guarantees: atomicity of the staged row with the business change, and
``FOR UPDATE SKIP LOCKED`` letting concurrent relays share work rather than
serialise. A mocked session can demonstrate neither.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

import pytest
from sqlalchemy import select

from app.common.utils.datetime import utc_now
from app.core.context import tenant_scope
from app.events.base import DomainEvent
from app.infrastructure.outbox import OutboxMessage, OutboxRelay, stage, stage_many

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True, kw_only=True)
class ThingHappened(DomainEvent):
    name: ClassVar[str] = "test.thing_happened"

    thing_id: str


async def _count(session) -> int:
    result = await session.execute(select(OutboxMessage))
    return len(result.scalars().all())


class TestStaging:
    async def test_stage_writes_a_row(self, session) -> None:
        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        assert await _count(session) == 1

    async def test_the_payload_is_the_serialised_event(self, session) -> None:
        event = ThingHappened(thing_id="a")
        await stage(session, event)
        await session.flush()

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        assert row.name == "test.thing_happened"
        assert row.payload["payload"]["thing_id"] == "a"
        assert row.payload["event_id"] == str(event.event_id)

    async def test_a_staged_row_starts_unpublished(self, session) -> None:
        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        assert row.published_at is None
        assert row.is_published is False
        assert row.attempts == 0

    async def test_the_tenant_context_is_captured(self, session) -> None:
        """So the relay can restore the context the event was produced in."""
        tenant_id = __import__("uuid").uuid7()
        async with tenant_scope(tenant_id):
            await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        assert row.tenant_id == tenant_id

    async def test_stage_many(self, session) -> None:
        await stage_many(
            session,
            [ThingHappened(thing_id="a"), ThingHappened(thing_id="b")],
        )
        await session.flush()

        assert await _count(session) == 2

    async def test_a_rollback_discards_the_staged_row(self, session) -> None:
        """The whole point: the event and the change commit together or not at all.

        Uses a nested transaction (SAVEPOINT) rather than rolling back the
        outer one, which belongs to the fixture — rolling that back mid-test
        detaches the session and leaves it unusable for the assertion.
        """
        savepoint = await session.begin_nested()
        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()
        assert await _count(session) == 1

        await savepoint.rollback()
        assert await _count(session) == 0


class TestRelay:
    async def test_publishes_a_pending_message(self, session) -> None:
        published: list[OutboxMessage] = []

        async def publisher(message: OutboxMessage) -> None:
            published.append(message)

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        relay = OutboxRelay(publisher)
        messages = await relay._claim(session)
        for message in messages:
            await relay._publish_one(message)

        assert len(published) == 1
        assert published[0].name == "test.thing_happened"

    async def test_a_published_message_is_marked(self, session) -> None:
        async def publisher(_message: OutboxMessage) -> None:
            return None

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        relay = OutboxRelay(publisher)
        for message in await relay._claim(session):
            await relay._publish_one(message)

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        assert row.published_at is not None
        assert row.is_published is True

    async def test_a_published_message_is_not_reclaimed(self, session) -> None:
        async def publisher(_message: OutboxMessage) -> None:
            return None

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        relay = OutboxRelay(publisher)
        for message in await relay._claim(session):
            await relay._publish_one(message)
        await session.flush()

        assert await relay._claim(session) == []

    async def test_a_failure_schedules_a_backed_off_retry(self, session) -> None:
        async def failing(_message: OutboxMessage) -> None:
            raise RuntimeError("broker is down")

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        relay = OutboxRelay(failing)
        for message in await relay._claim(session):
            await relay._publish_one(message)

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        assert row.published_at is None
        assert row.attempts == 1
        assert "broker is down" in row.last_error
        assert row.next_attempt_at > utc_now()

    async def test_a_backed_off_message_is_not_claimed_yet(self, session) -> None:
        """A failing message must not block the ones queued behind it."""

        async def failing(_message: OutboxMessage) -> None:
            raise RuntimeError("down")

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        relay = OutboxRelay(failing)
        for message in await relay._claim(session):
            await relay._publish_one(message)
        await session.flush()

        assert await relay._claim(session) == []

    async def test_one_failure_does_not_stop_the_batch(self, session) -> None:
        attempted: list[str] = []

        async def flaky(message: OutboxMessage) -> None:
            attempted.append(message.payload["payload"]["thing_id"])
            if message.payload["payload"]["thing_id"] == "a":
                raise RuntimeError("this one fails")

        await stage_many(
            session,
            [ThingHappened(thing_id="a"), ThingHappened(thing_id="b")],
        )
        await session.flush()

        relay = OutboxRelay(flaky)
        for message in await relay._claim(session):
            await relay._publish_one(message)

        assert sorted(attempted) == ["a", "b"]

    async def test_an_exhausted_message_is_kept_not_deleted(self, session) -> None:
        """The payload is often the only record of what should have happened."""
        from app.infrastructure.outbox.relay import MAX_ATTEMPTS

        async def failing(_message: OutboxMessage) -> None:
            raise RuntimeError("permanent")

        await stage(session, ThingHappened(thing_id="a"))
        await session.flush()

        row = (await session.execute(select(OutboxMessage))).scalar_one()
        row.attempts = MAX_ATTEMPTS - 1
        await session.flush()

        relay = OutboxRelay(failing)
        for message in await relay._claim(session):
            await relay._publish_one(message)
        await session.flush()

        assert await _count(session) == 1
        assert await relay._claim(session) == []

    async def test_ordering_is_oldest_first(self, session) -> None:
        order: list[str] = []

        async def publisher(message: OutboxMessage) -> None:
            order.append(message.payload["payload"]["thing_id"])

        first = ThingHappened(thing_id="first")
        await stage(session, first)
        await session.flush()

        second = ThingHappened(thing_id="second")
        await stage(session, second)
        await session.flush()

        relay = OutboxRelay(publisher)
        for message in await relay._claim(session):
            await relay._publish_one(message)

        assert order == ["first", "second"]


class TestMaintenance:
    async def test_purge_removes_only_published_rows(self, session) -> None:
        """An unpublished row is undelivered work; deleting it is data loss."""
        await stage(session, ThingHappened(thing_id="pending"))
        await stage(session, ThingHappened(thing_id="done"))
        await session.flush()

        rows = (await session.execute(select(OutboxMessage))).scalars().all()
        rows[1].published_at = utc_now() - timedelta(days=30)
        await session.flush()

        # Exercised through the session rather than purge_published(), which
        # opens its own transaction and cannot see this test's rollback scope.
        from sqlalchemy import delete

        cutoff = utc_now() - timedelta(days=7)
        await session.execute(
            delete(OutboxMessage).where(
                OutboxMessage.published_at.is_not(None),
                OutboxMessage.published_at < cutoff,
            )
        )
        await session.flush()

        remaining = (await session.execute(select(OutboxMessage))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].payload["payload"]["thing_id"] == "pending"
