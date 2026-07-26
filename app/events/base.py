"""Domain event primitives.

Why this file exists
--------------------
Features accumulate cross-cutting reactions: when a subscription is cancelled,
billing stops, an email goes out, analytics records it, and the workspace is
downgraded. Calling all four services from the subscription service couples it
to every one of them, and each new reaction edits code that has nothing to do
with the new feature.

Events invert that dependency. The publisher states what *happened*; interested
modules subscribe. The subscription module then knows nothing about billing or
email, and a new reaction is a new subscriber, not an edit.

When **not** to use an event
----------------------------
When the caller needs the result, or when the reaction must be part of the same
transaction. Events describe the past and cannot be "cancelled" by a handler —
a validation check must be a direct call, not a listener.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from app.common.utils.datetime import utc_now


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class for every domain event.

    Immutable and named in the past tense (``UserRegistered``, not
    ``RegisterUser``) — an event is a statement of fact, and a handler that
    could mutate it would change what other handlers see.

    Attributes:
        name: Stable wire identifier, e.g. ``"billing.invoice_paid"``. Class
            names are refactored; this is not, because it may cross a process
            boundary.
        version: Schema version, incremented on any breaking payload change so
            consumers can reject what they cannot read.
        event_id: Unique per occurrence. Handlers use it to deduplicate under
            at-least-once delivery.
        occurred_at: When the fact became true.
    """

    name: ClassVar[str] = "domain.event"
    version: ClassVar[int] = 1

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=utc_now)

    def to_payload(self) -> dict[str, Any]:
        """Serialise the event for transport or persistence.

        Must produce only JSON-representable values: an event may be published
        to Redis or written to an outbox table and read by another service, or
        another language.
        """
        raise NotImplementedError


# TODO: add an `IntegrationEvent` subclass for events that leave the process,
# so the internal/external boundary is explicit and only external events are
# bound by a compatibility guarantee.
