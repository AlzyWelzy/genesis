"""End-to-end delivery for events staged with ``publish_after_commit``.

Why these exist
---------------
``publish_after_commit(session, event)`` buffers events on the session and
relies on something calling ``flush_pending_events`` after the commit. Nothing
did. The default — non-durable — mode of the whole event system therefore
delivered nothing: a service staged an event, committed, returned 200, and no
handler ran. No exception, no log line, no failed test, because every existing
test called ``EventBus.publish`` directly and so never traversed the staging
path at all.

These tests go through the real session helpers instead of the bus, which is the
only way that gap is visible. The ordering guarantees are asserted too — an
event must not escape before its commit, and must not escape at all if the
transaction rolled back, because a subscriber reacting to something that never
happened is worse than one that missed it.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import pytest

from app.events.base import DomainEvent
from app.events.bus import event_bus, publish_after_commit
from app.infrastructure.database.session import session_scope

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True, kw_only=True)
class ThingHappened(DomainEvent):
    """A test event carrying one field."""

    name: ClassVar[str] = "test.thing_happened"

    label: str


@pytest.fixture
def received(migrated_database) -> Iterator[list[ThingHappened]]:
    """Subscribe a recording handler, and unsubscribe it afterwards.

    Subscriptions are process-wide global state, so leaving one registered
    would leak into every test that runs later.
    """
    collected: list[ThingHappened] = []

    async def handler(event: ThingHappened) -> None:
        collected.append(event)

    event_bus.subscribe(ThingHappened)(handler)
    yield collected
    event_bus.clear()


class TestSessionScope:
    """The worker and script path."""

    async def test_a_staged_event_is_delivered(self, received) -> None:
        """The bug this file exists for: nothing called the flush."""
        async with session_scope() as session:
            await publish_after_commit(session, ThingHappened(label="a"))

        assert [event.label for event in received] == ["a"]

    async def test_events_are_delivered_in_order(self, received) -> None:
        """Events from one unit of work usually describe a sequence."""
        async with session_scope() as session:
            await publish_after_commit(
                session,
                ThingHappened(label="first"),
                ThingHappened(label="second"),
            )

        assert [event.label for event in received] == ["first", "second"]

    async def test_nothing_is_delivered_before_the_commit(self, received) -> None:
        """A handler reading the row would not find it yet."""
        async with session_scope() as session:
            await publish_after_commit(session, ThingHappened(label="a"))
            assert received == []

        assert len(received) == 1

    async def test_a_rollback_delivers_nothing(self, received) -> None:
        """Reacting to something that never happened is worse than missing it."""

        async def stage_then_fail() -> None:
            async with session_scope() as session:
                await publish_after_commit(session, ThingHappened(label="a"))
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await stage_then_fail()

        assert received == []

    async def test_a_failing_handler_does_not_fail_the_transaction(
        self, received
    ) -> None:
        """The commit already happened; the caller cannot act on the failure."""

        async def broken(_event: ThingHappened) -> None:
            raise ValueError("handler is broken")

        event_bus.subscribe(ThingHappened)(broken)

        async with session_scope() as session:
            await publish_after_commit(session, ThingHappened(label="a"))

        # The working handler still ran, despite the broken one.
        assert [event.label for event in received] == ["a"]

    async def test_events_do_not_leak_between_sessions(self, received) -> None:
        """The buffer lives in ``session.info``, so its lifetime is the session's."""
        async with session_scope() as session:
            await publish_after_commit(session, ThingHappened(label="a"))

        async with session_scope():
            pass

        assert [event.label for event in received] == ["a"]

    async def test_an_event_is_published_only_once(self, received) -> None:
        """The buffer is cleared before publication, not after."""
        async with session_scope() as session:
            await publish_after_commit(session, ThingHappened(label="a"))
            await session.commit()

        assert [event.label for event in received] == ["a"]


class TestDurableMode:
    """``durable=True`` routes through the outbox instead of the bus."""

    async def test_a_durable_event_does_not_publish_in_process(
        self, received, session
    ) -> None:
        """It is the relay's job, after the row commits — not the session's."""
        await publish_after_commit(session, ThingHappened(label="a"), durable=True)
        await session.flush()

        assert received == []

    async def test_a_durable_event_is_staged_in_the_outbox(self, session) -> None:
        from sqlalchemy import select

        from app.infrastructure.outbox.models import OutboxMessage

        await publish_after_commit(session, ThingHappened(label="a"), durable=True)
        await session.flush()

        result = await session.execute(
            select(OutboxMessage).where(OutboxMessage.name == "test.thing_happened")
        )
        staged = result.scalars().all()
        assert len(staged) == 1
        # The column holds the whole envelope from `to_payload()`, not the bare
        # body, so a consumer reading only this column can still tell which
        # event it has and which schema version it is.
        assert staged[0].payload["payload"]["label"] == "a"
        assert staged[0].payload["name"] == "test.thing_happened"
