"""In-process event bus.

Why this file exists
--------------------
:mod:`app.events.base` defines what an event *is*; this defines how it travels.
The bus is the indirection that lets a publisher stay ignorant of its
subscribers — without it, "publishing" is a direct call with extra steps.

Delivery semantics
------------------
* **Publish after commit.** A handler that reads the row the event refers to
  will not find it if the transaction has not committed. Collect events during
  a unit of work and flush them once the commit succeeds.
* **A failing handler must not fail the publisher.** The publisher's work is
  already done and committed; an exception from an unrelated listener is logged,
  never propagated. The alternative — one broken analytics subscriber failing
  the request that triggered it — is strictly worse.
* **Handlers must be idempotent.** Retries and at-least-once delivery mean any
  handler can run twice for one ``event_id``.
* **Long work belongs in the queue.** A handler should validate and enqueue,
  not perform a thirty-second export inline.

Scope
-----
In-process and concurrent with respect to the publisher's task. Sufficient for
a single-service deployment. When handlers must run in another process, back
this with :mod:`app.infrastructure.redis.pubsub` (broadcast) or
:mod:`app.infrastructure.queue` (durable work) behind the same interface, so
publishers never change.
"""

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger
from app.events.base import DomainEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

#: Key under which buffered events live in ``session.info``.
_PENDING_EVENTS_KEY: Final[str] = "genesis.pending_events"


def _describe(handler: object) -> str:
    """Name a handler for logs.

    ``Callable`` carries no ``__qualname__`` in the type system even though
    every real function has one, so read it defensively.
    """
    return getattr(handler, "__qualname__", repr(handler))


#: A subscriber. Returns nothing; raising is logged and swallowed by the bus.
type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


class EventBus:
    """Routes published events to their registered handlers."""

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[EventHandler[DomainEvent]]] = (
            defaultdict(list)
        )

    def subscribe[E: DomainEvent](
        self, event_type: type[E]
    ) -> Callable[[EventHandler[E]], EventHandler[E]]:
        """Decorator registering a handler for an event type.

        Registration happens at import time, so the module declaring handlers
        must be imported during startup — see :mod:`app.core.lifespan`. A
        decorator that never runs is a subscription that silently does not
        exist, which is the most common way an event system appears broken.

        Args:
            event_type: The event class to listen for.

        Returns:
            The decorator, which returns the handler unchanged so it stays
            directly callable in tests.

        Raises:
            TypeError: When the handler is not a coroutine function. A sync
                handler would block the event loop for every publisher.
        """

        def decorator(handler: EventHandler[E]) -> EventHandler[E]:
            if not inspect.iscoroutinefunction(handler):
                raise TypeError(
                    f"Event handler {_describe(handler)} must be async; a "
                    "synchronous handler blocks the event loop for every publisher."
                )
            self._handlers[event_type].append(handler)  # ty: ignore[invalid-argument-type]
            logger.debug("Subscribed %s to %s", _describe(handler), event_type.__name__)
            return handler

        return decorator

    def register[E: DomainEvent](
        self, event_type: type[E], handler: EventHandler[E]
    ) -> None:
        """Register a handler without the decorator syntax.

        For wiring subscriptions explicitly at startup, and for tests that need
        to attach a handler to a bus they built themselves.
        """
        self.subscribe(event_type)(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Deliver an event to every handler registered for its type.

        Handlers run concurrently. Exceptions are logged and contained: one
        broken subscriber must not prevent the others from running, nor surface
        as an error to the publisher.

        Handlers registered against a *base* class also receive subclass
        events, so a listener can subscribe to :class:`DomainEvent` itself to
        observe everything — which is how a generic audit-log subscriber works.

        Args:
            event: The event to publish. Must be published after commit.
        """
        handlers = [
            handler
            for event_type, registered in self._handlers.items()
            if isinstance(event, event_type)
            for handler in registered
        ]
        if not handlers:
            logger.debug("No handlers for %s", type(event).__name__)
            return

        results = await asyncio.gather(
            *(handler(event) for handler in handlers), return_exceptions=True
        )

        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                logger.exception(
                    "Event handler failed",
                    exc_info=result,
                    extra={
                        "event_name": event.name,
                        "event_id": str(event.event_id),
                        "handler": _describe(handler),
                    },
                )

    async def publish_many(self, events: list[DomainEvent]) -> None:
        """Publish several events in order.

        Sequential rather than concurrent: events from one unit of work often
        describe a sequence, and a handler for the second may depend on the
        first having been processed.
        """
        for event in events:
            await self.publish(event)

    def handler_count(self, event_type: type[DomainEvent]) -> int:
        """Return how many handlers are registered for an exact event type.

        For startup assertions and tests. A count of zero for an event the
        application publishes usually means the declaring module was never
        imported.
        """
        return len(self._handlers.get(event_type, []))

    def clear(self) -> None:
        """Remove every subscription.

        For tests only. Subscriptions are import-time global state, so a test
        that registers a handler would otherwise leak it into every test that
        runs afterwards.
        """
        self._handlers.clear()


#: Process-wide bus. Import this rather than constructing another.
event_bus = EventBus()


async def publish_after_commit(
    session: AsyncSession, *events: DomainEvent, durable: bool = False
) -> None:
    """Publish events once the caller's transaction commits.

    Solves the ordering problem that makes naive publishing wrong: a handler
    that reads the row an event refers to will not find it if the transaction
    has not committed yet, and publishing *before* the commit means a rollback
    leaves subscribers reacting to something that never happened.

    Two modes, and the choice is a real one:

    **``durable=False``** (default) stages the events on the session and
    publishes them in-process after the commit succeeds. Cheap, and the
    ordering is correct — but a crash in the window between commit and publish
    loses them.

    **``durable=True``** writes the events to the outbox *inside* the
    transaction, so they commit atomically with the business change and a relay
    publishes them afterwards. Costs one insert per event and requires the relay
    to be running. Use it for anything whose loss a customer would notice —
    payments, receipts, provisioning — and leave it off for cache invalidation,
    where the next write republishes anyway.

    Args:
        session: The session running the business transaction.
        *events: Events to publish.
        durable: Route through the transactional outbox.
    """
    if durable:
        # Deferred: outbox.relay imports events.base, so a top-level import
        # here would close a cycle. The durable path is the rarer one, so
        # paying an import lookup on it is the right trade.
        from app.infrastructure.outbox.relay import (  # noqa: PLC0415
            stage_many,
        )

        await stage_many(session, list(events))
        return

    _pending_events(session).extend(events)


def _pending_events(session: AsyncSession) -> list[DomainEvent]:
    """Return the event buffer attached to a session, creating it on demand.

    Stored in ``session.info``, SQLAlchemy's per-session scratch space, so the
    buffer's lifetime is exactly the session's and two concurrent requests
    cannot see each other's events.
    """
    return session.info.setdefault(_PENDING_EVENTS_KEY, [])


async def flush_pending_events(session: AsyncSession) -> int:
    """Publish the events buffered on a session, after its commit.

    Call this immediately after ``session.commit()``. Buffered events are
    cleared before publication, so a handler that raises cannot cause the same
    event to be published twice on a later flush.

    Returns:
        How many events were published.
    """
    pending = _pending_events(session)
    if not pending:
        return 0

    events = list(pending)
    pending.clear()
    await event_bus.publish_many(events)
    return len(events)
