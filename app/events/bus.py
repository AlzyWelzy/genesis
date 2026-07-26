"""In-process event bus.

Why this file exists
--------------------
:mod:`app.events.base` defines what an event *is*; this defines how it travels.
The bus is the indirection that lets a publisher stay ignorant of its
subscribers — without it, "publishing" is just a direct call with extra steps.

Delivery semantics
------------------
* **Publish after commit.** A handler that reads the row the event refers to
  will not find it if the transaction has not committed. Collect events during
  a unit of work and flush them once the commit succeeds.
* **A failing handler must not fail the publisher.** The publisher's work is
  already done and committed; an exception from an unrelated listener must be
  logged, not propagated.
* **Handlers must be idempotent.** Retries and at-least-once delivery mean any
  handler can run twice for one ``event_id``.
* **Long work belongs in the queue.** A handler should validate and enqueue,
  not perform a thirty-second export inline.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable

from app.core.logging import get_logger
from app.events.base import DomainEvent

logger = get_logger(__name__)

#: A subscriber. Returns nothing; raising is logged and swallowed by the bus.
type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


class EventBus:
    """Routes published events to their registered handlers.

    In-process and synchronous with respect to the publisher's task. That is
    sufficient for a single-service deployment; when handlers must run in
    another process, back this with
    :mod:`app.infrastructure.redis.pubsub` (broadcast) or
    :mod:`app.infrastructure.queue` (durable work) behind the same interface,
    so publishers never change.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[EventHandler[DomainEvent]]] = (
            defaultdict(list)
        )

    def subscribe[E: DomainEvent](
        self, event_type: type[E]
    ) -> Callable[[EventHandler[E]], EventHandler[E]]:
        """Decorator registering a handler for an event type.

        Registration happens at import time, so the module declaring handlers
        must be imported during startup — see :mod:`app.core.lifespan`.
        """
        raise NotImplementedError

    async def publish(self, event: DomainEvent) -> None:
        """Deliver an event to every handler registered for its type.

        Handlers run concurrently. Exceptions are logged and contained: one
        broken subscriber must not prevent the others from running, nor
        surface as an error to the publisher.
        """
        raise NotImplementedError

    async def publish_many(self, events: list[DomainEvent]) -> None:
        """Publish several events in order."""
        raise NotImplementedError


#: Process-wide bus. Import this rather than constructing another.
event_bus = EventBus()

# TODO: implement a transactional outbox — write events to a table inside the
# business transaction and publish them from a relay — once losing an event on
# a crash between commit and publish becomes unacceptable.
