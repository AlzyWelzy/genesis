"""Staging events into the outbox, and the relay that publishes them.

Why this file exists
--------------------
:mod:`app.infrastructure.outbox.models` defines where an unpublished event
lives. This defines the two halves of the mechanism around it:

* :func:`stage` — called inside a business transaction, so the intent to
  publish commits atomically with the change it describes.
* :class:`OutboxRelay` — a background loop that publishes staged rows and marks
  them done.

Delivery guarantee
------------------
**At least once.** A crash between publishing and marking the row published
republishes on the next pass. That is not a flaw to be engineered away — the
alternative (mark first, then publish) loses messages instead, and losing is
worse than repeating. Every handler must therefore be idempotent, keyed on the
event ID.

Concurrency
-----------
Several relay instances may run — during a rolling deploy there will be at
least two. Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED``, so each row is
taken by exactly one relay and the others move past it rather than blocking.
Without ``SKIP LOCKED`` the relays serialise behind each other and adding
instances makes throughput worse, not better.

Ordering
--------
Rows are claimed oldest-first, but publication is **not** globally ordered:
concurrent relays finish at different speeds. Anything requiring strict
per-entity ordering must carry a sequence number in its payload and let the
consumer reorder. Do not assume the outbox provides ordering it cannot.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.utils.datetime import utc_now
from app.core.context import get_correlation_id, get_tenant_id
from app.core.logging import get_logger
from app.events.base import DomainEvent
from app.infrastructure.database.session import session_scope
from app.infrastructure.outbox.models import OutboxMessage

logger = get_logger(__name__)

#: Publishes one outbox message to wherever it should go. Injected rather than
#: imported, so the destination is the caller's choice and the relay stays
#: testable without Redis.
type OutboxPublisher = Callable[[OutboxMessage], Awaitable[None]]

#: Rows claimed per pass. Small enough that a crash re-does little work, large
#: enough that the per-pass query overhead is amortised.
BATCH_SIZE = 100

#: Pause between passes when the last one found nothing. Polling rather than
#: LISTEN/NOTIFY: polling one indexed partial index every second is cheap and
#: has no failure mode, whereas NOTIFY is lost if no listener is connected —
#: which is exactly the situation during a deploy.
IDLE_SLEEP_SECONDS = 1.0

#: Attempts before a message is left alone for a human. It is *not* deleted:
#: the payload is often the only record of what should have happened.
MAX_ATTEMPTS = 10

#: Cap on retry backoff, so a message that starts working again is picked up
#: within a few minutes rather than hours.
MAX_BACKOFF_SECONDS = 300

#: Truncation for the stored error. Enough to identify the failure; a full
#: traceback belongs in the logs, not in every row of a large table.
MAX_ERROR_LENGTH = 500


async def stage(session: AsyncSession, event: DomainEvent) -> OutboxMessage:
    """Record an event for publication inside the caller's transaction.

    **Must be called before the commit, on the same session as the business
    write.** That is the entire point: the row and the change it describes
    become durable together or not at all. Staging on a different session, or
    after the commit, reintroduces the gap this exists to close.

    The current tenant and correlation ID are captured from the ambient
    context, so the relay — and the worker beyond it — can restore the context
    the event was produced in and keep the log trail connected.

    Args:
        session: The session running the business transaction.
        event: The event to publish once the transaction commits.

    Returns:
        The staged row, not yet flushed.
    """
    message = OutboxMessage(
        name=event.name,
        version=event.version,
        payload=event.to_payload(),
        tenant_id=get_tenant_id(),
        correlation_id=get_correlation_id(),
        occurred_at=event.occurred_at,
    )
    session.add(message)
    return message


async def stage_many(
    session: AsyncSession, events: Sequence[DomainEvent]
) -> list[OutboxMessage]:
    """Stage several events in one transaction."""
    return [await stage(session, event) for event in events]


class OutboxRelay:
    """Publishes staged messages and marks them delivered.

    Args:
        publish: Called with each claimed message. Should be idempotent and
            should raise on failure so the row is retried. Injected rather than
            imported so the relay stays testable without Redis, and so the
            destination (event bus, queue, pub/sub) is the caller's choice.
        batch_size: Rows claimed per pass.
    """

    def __init__(
        self,
        publish: OutboxPublisher,
        *,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._publish = publish
        self._batch_size = batch_size
        self._running = False

    async def run(self) -> None:
        """Publish continuously until stopped."""
        self._running = True
        logger.info("Outbox relay started", extra={"batch_size": self._batch_size})

        while self._running:
            try:
                published = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbox relay pass failed; continuing")
                published = 0

            if published == 0:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)

    def stop(self) -> None:
        """Ask the loop to finish after the current pass."""
        self._running = False

    async def run_once(self) -> int:
        """Claim and publish one batch.

        Returns:
            How many messages were published successfully.
        """
        async with session_scope() as session:
            messages = await self._claim(session)
            if not messages:
                return 0

            published = 0
            for message in messages:
                if await self._publish_one(message):
                    published += 1
            return published

    async def _claim(self, session: AsyncSession) -> Sequence[OutboxMessage]:
        """Claim a batch of due messages for this relay.

        ``FOR UPDATE SKIP LOCKED`` is what makes multiple relays safe: a row
        already locked by another instance is skipped rather than waited on, so
        instances share the work instead of serialising behind each other.
        """
        now = utc_now()
        statement = (
            select(OutboxMessage)
            .where(
                OutboxMessage.published_at.is_(None),
                OutboxMessage.attempts < MAX_ATTEMPTS,
                (OutboxMessage.next_attempt_at.is_(None))
                | (OutboxMessage.next_attempt_at <= now),
            )
            .order_by(OutboxMessage.created_at)
            .limit(self._batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(statement)
        return result.scalars().all()

    async def _publish_one(self, message: OutboxMessage) -> bool:
        """Publish one message, recording the outcome.

        Returns:
            ``True`` when published.
        """
        try:
            await self._publish(message)
        except Exception as exc:  # noqa: BLE001 - one message must not stop the batch
            await self._record_failure(message, exc)
            return False

        message.published_at = utc_now()
        message.last_error = None
        logger.debug(
            "Outbox message published",
            extra={"event_name": message.name, "message_id": str(message.id)},
        )
        return True

    async def _record_failure(self, message: OutboxMessage, exc: Exception) -> None:
        """Increment the attempt counter and schedule a backed-off retry."""
        message.attempts += 1
        message.last_error = str(exc)[:MAX_ERROR_LENGTH]

        delay = min(2**message.attempts, MAX_BACKOFF_SECONDS)
        message.next_attempt_at = utc_now() + timedelta(seconds=delay)

        if message.attempts >= MAX_ATTEMPTS:
            # Left in place rather than deleted. The payload is frequently the
            # only surviving record of what was supposed to happen, and it is
            # what a replay needs once the bug is fixed. Alert on this count.
            logger.error(
                "Outbox message exhausted its attempts",
                exc_info=exc,
                extra={"event_name": message.name, "message_id": str(message.id)},
            )
        else:
            logger.warning(
                "Outbox publication failed; will retry",
                exc_info=exc,
                extra={
                    "event_name": message.name,
                    "attempts": message.attempts,
                    "retry_in_seconds": delay,
                },
            )


def _rows_affected(result: Result[Any]) -> int:
    """Read the affected-row count from a DML result.

    ``rowcount`` is only defined on cursor-backed results, which SQLAlchemy's
    generic ``Result`` type does not promise; reading it defensively keeps the
    checker honest without pretending the attribute is always there.
    """
    return getattr(result, "rowcount", 0) or 0


async def purge_published(older_than: timedelta) -> int:
    """Delete published rows beyond a retention window.

    Without this the table grows without bound, and while the partial index
    keeps the relay fast, backups, vacuum and disk do not care about indexes.

    Deleting only *published* rows is essential: an unpublished row is
    undelivered work, and removing it is silent data loss.

    Args:
        older_than: Retention window measured from publication.

    Returns:
        How many rows were deleted.
    """
    cutoff = utc_now() - older_than
    async with session_scope() as session:
        result = await session.execute(
            delete(OutboxMessage).where(
                OutboxMessage.published_at.is_not(None),
                OutboxMessage.published_at < cutoff,
            )
        )
        deleted = _rows_affected(result)

    if deleted:
        logger.info("Purged published outbox messages", extra={"deleted": deleted})
    return deleted


async def pending_count() -> int:
    """Count messages awaiting publication.

    The single most useful outbox metric. A number that climbs means the relay
    is down, wedged, or slower than the write rate — and every one of those is
    a customer-visible failure a few minutes later. Alert on it.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
        )
        return result.scalar_one()


async def reset_stalled(older_than: timedelta) -> int:
    """Clear the backoff on messages that have been waiting too long.

    An operator's lever after fixing whatever was breaking publication: without
    it, a message that backed off to five minutes keeps waiting even though the
    cause is gone.

    Args:
        older_than: Only touch messages created before this cutoff.

    Returns:
        How many messages were rescheduled.
    """
    cutoff = utc_now() - older_than
    async with session_scope() as session:
        result = await session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.published_at.is_(None),
                OutboxMessage.created_at < cutoff,
            )
            .values(next_attempt_at=None, attempts=0)
        )
        return _rows_affected(result)
