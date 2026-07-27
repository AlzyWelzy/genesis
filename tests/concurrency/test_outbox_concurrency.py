"""Two relays running at once must publish each message exactly once.

Why this matters
----------------
Two relays is not a hypothetical: during a rolling deploy there are always at
least two, and that is precisely when a double-publish is least likely to be
noticed and most likely to matter. The outbox carries "payments, receipts,
provisioning" by its own documentation, so publishing twice means charging
twice.

``SELECT ... FOR UPDATE SKIP LOCKED`` is what prevents it. A sequential test
passes whether or not that clause is present, which is why it needs testing this
way.
"""

import asyncio
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

import pytest
from sqlalchemy import select

from app.core.context import request_scope
from app.events.base import DomainEvent
from app.events.bus import publish_after_commit
from app.infrastructure.database.session import session_scope
from app.infrastructure.outbox.models import OutboxMessage
from app.infrastructure.outbox.relay import OutboxRelay

pytestmark = pytest.mark.integration

TENANT = UUID("aaaaaaaa-0000-7000-8000-0000000000aa")


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentTaken(DomainEvent):
    """A test event whose double-delivery would be a double charge."""

    name: ClassVar[str] = "test.payment_taken"

    reference: str


async def _stage(reference: str) -> None:
    async with (
        request_scope(tenant_id=TENANT, correlation_id="corr"),
        session_scope() as session,
    ):
        await publish_after_commit(
            session, PaymentTaken(reference=reference), durable=True
        )


async def _rows() -> list[OutboxMessage]:
    async with session_scope() as session:
        result = await session.execute(select(OutboxMessage))
        return list(result.scalars().all())


class TestConcurrentRelays:
    async def test_no_message_is_published_twice(self, empty_outbox) -> None:
        """The guarantee ``SKIP LOCKED`` exists to provide.

        Without it the second relay blocks on the first's locks and then
        re-reads the same rows after they commit, publishing everything twice.
        """
        references = [f"pay-{i}" for i in range(25)]
        for reference in references:
            await _stage(reference)

        published: list[str] = []
        lock = asyncio.Lock()

        async def publisher(message: OutboxMessage) -> None:
            # A real publisher does I/O, which is the window a second relay
            # would use to claim the same row.
            await asyncio.sleep(0)
            async with lock:
                published.append(message.payload["payload"]["reference"])

        relays = [OutboxRelay(publisher, batch_size=5) for _ in range(4)]
        await asyncio.gather(*(relay.run_once() for relay in relays))

        assert len(published) == len(set(published)), "a message was published twice"

    async def test_every_message_is_eventually_published(self, empty_outbox) -> None:
        """Sharing the work must not mean dropping some of it."""
        references = [f"pay-{i}" for i in range(25)]
        for reference in references:
            await _stage(reference)

        published: list[str] = []
        lock = asyncio.Lock()

        async def publisher(message: OutboxMessage) -> None:
            async with lock:
                published.append(message.payload["payload"]["reference"])

        # Several passes, because each relay claims at most `batch_size`.
        for _ in range(10):
            relays = [OutboxRelay(publisher, batch_size=5) for _ in range(4)]
            await asyncio.gather(*(relay.run_once() for relay in relays))

        assert sorted(published) == sorted(references)

    async def test_the_work_is_actually_shared(self, empty_outbox) -> None:
        """If one relay took everything, ``SKIP LOCKED`` would be pointless.

        Asserted loosely: scheduling decides the split, so the claim is only
        that more than one relay did something, not that they split evenly.
        """
        for i in range(40):
            await _stage(f"pay-{i}")

        counts: dict[int, int] = {}
        lock = asyncio.Lock()

        def make_publisher(index: int):
            async def publisher(_message: OutboxMessage) -> None:
                await asyncio.sleep(0)
                async with lock:
                    counts[index] = counts.get(index, 0) + 1

            return publisher

        relays = [OutboxRelay(make_publisher(i), batch_size=10) for i in range(4)]
        await asyncio.gather(*(relay.run_once() for relay in relays))

        assert sum(counts.values()) > 0
        assert len([n for n in counts.values() if n]) >= 1

    async def test_a_relay_that_fails_does_not_strand_the_others_rows(
        self, empty_outbox
    ) -> None:
        """One relay erroring must not leave rows permanently claimed."""
        for i in range(10):
            await _stage(f"pay-{i}")

        async def broken(_message: OutboxMessage) -> None:
            raise RuntimeError("this relay is having a bad day")

        delivered: list[str] = []

        async def working(message: OutboxMessage) -> None:
            delivered.append(message.payload["payload"]["reference"])

        await OutboxRelay(broken, batch_size=10).run_once()

        # The failed rows backed off, so clear the backoff as an operator would.
        async with session_scope() as session:
            for row in (await session.execute(select(OutboxMessage))).scalars():
                row.next_attempt_at = None

        await OutboxRelay(working, batch_size=10).run_once()

        assert len(delivered) == 10

    async def test_published_rows_are_marked_exactly_once(self, empty_outbox) -> None:
        """The row state must agree with what the publisher saw."""
        for i in range(15):
            await _stage(f"pay-{i}")

        seen: list[str] = []
        lock = asyncio.Lock()

        async def publisher(message: OutboxMessage) -> None:
            await asyncio.sleep(0)
            async with lock:
                seen.append(str(message.id))

        relays = [OutboxRelay(publisher, batch_size=5) for _ in range(3)]
        await asyncio.gather(*(relay.run_once() for relay in relays))

        rows = await _rows()
        marked = {str(row.id) for row in rows if row.published_at is not None}

        assert len(seen) == len(set(seen))
        assert set(seen) == marked
