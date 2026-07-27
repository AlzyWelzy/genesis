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
email, and a new reaction is a new subscriber rather than an edit.

When **not** to use an event
----------------------------
When the caller needs the result, or when the reaction must be part of the same
transaction. Events describe the past and cannot be "cancelled" by a handler —
a validation check must be a direct call, not a listener.
"""

import base64
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Any, ClassVar
from uuid import UUID, uuid7

from app.common.utils.datetime import utc_now

#: Fields owned by the envelope rather than by a concrete event's payload.
_ENVELOPE_FIELDS = frozenset({"event_id", "occurred_at"})


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class for every domain event.

    Immutable and named in the past tense (``UserRegistered``, not
    ``RegisterUser``) — an event is a statement of fact, and a handler that
    could mutate it would change what other handlers see.

    Subclasses declare their own payload fields::

        @dataclass(frozen=True, slots=True, kw_only=True)
        class InvoicePaid(DomainEvent):
            name: ClassVar[str] = "billing.invoice_paid"

            invoice_id: UUID
            amount: Decimal

    Attributes:
        name: Stable wire identifier, e.g. ``"billing.invoice_paid"``. Class
            names get refactored; this does not, because it may cross a process
            boundary and be matched by a consumer written elsewhere.
        version: Schema version, incremented on any breaking payload change so
            consumers can reject what they cannot read.
        event_id: Unique per occurrence. Handlers use it to deduplicate under
            at-least-once delivery.
        occurred_at: When the fact became true.
    """

    name: ClassVar[str] = "domain.event"
    version: ClassVar[int] = 1

    event_id: UUID = field(default_factory=uuid7)
    occurred_at: datetime = field(default_factory=utc_now)

    def payload(self) -> dict[str, Any]:
        """Return only the subclass's own fields, JSON-ready.

        Excludes the envelope fields, which :meth:`to_payload` carries
        separately, so a consumer sees a clean separation between "which event
        is this" and "what does it say".
        """
        return {
            name: _to_jsonable(value)
            for name, value in asdict(self).items()
            if name not in _ENVELOPE_FIELDS
        }

    def to_payload(self) -> dict[str, Any]:
        """Serialise the event for transport or persistence.

        Produces only JSON-representable values: an event may be published to
        Redis, written to an outbox table, and read by another service — or
        another language — so ``UUID``, ``datetime``, ``Decimal`` and ``Enum``
        are all converted to strings rather than left as Python objects.

        Returns:
            The full envelope: identity, version, timing and payload.
        """
        return {
            "event_id": str(self.event_id),
            "name": self.name,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload(),
        }

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return this event's payload field names, excluding the envelope."""
        return tuple(f.name for f in fields(cls) if f.name not in _ENVELOPE_FIELDS)


#: Scalar conversions applied by :func:`_to_jsonable`. ``Decimal`` becomes a
#: string, never a float: binary floats cannot represent decimal fractions
#: exactly, and an event carrying money must round-trip to the cent.
#:
#: **Order matters.** ``datetime`` is a subclass of ``date``, so it must be
#: matched first or every timestamp would be truncated to a bare day — a silent
#: loss of the time component, which is far worse than a crash.
#:
#: ``date`` and ``timedelta`` are here because they are ordinary things for a
#: domain event to carry (``due_date``, ``retention_period``) and neither is
#: JSON-serialisable. Left out, staging such an event to the outbox fails when
#: asyncpg encodes the JSONB column — which happens *inside* the business
#: transaction, so adding a due date to an event breaks the write it describes.
# The key may be a union (``bytes | bytearray``), which ``isinstance`` accepts
# and is narrower to read than two entries sharing one encoder.
_SCALAR_ENCODERS: tuple[tuple[type | UnionType, Callable[[Any], Any]], ...] = (
    (datetime, lambda value: value.isoformat()),
    (date, lambda value: value.isoformat()),
    (timedelta, lambda value: value.total_seconds()),
    (UUID, str),
    (Decimal, str),
    (Enum, lambda value: value.value),
    # Base64 rather than a decode attempt: bytes in an event are usually a
    # digest or a signature, and `.decode()` would raise on the first one that
    # is not valid UTF-8.
    (bytes | bytearray, lambda value: base64.b64encode(value).decode("ascii")),
)


def _to_jsonable(value: Any) -> Any:
    """Convert a value into something ``json.dumps`` accepts.

    Deliberately conservative: it handles the types that actually appear in
    domain payloads and leaves anything else alone, so an unserialisable value
    fails loudly at the encoder rather than being silently stringified into
    something a consumer cannot parse back.
    """
    for scalar_type, encode in _SCALAR_ENCODERS:
        if isinstance(value, scalar_type):
            return encode(value)
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationEvent(DomainEvent):
    """An event that leaves this process.

    The distinction from a plain :class:`DomainEvent` is a *contract*
    commitment, not a technical one. An internal event can be renamed or
    reshaped in the same commit as its only subscriber; an integration event is
    consumed by systems deployed on someone else's schedule, so its payload is
    subject to the same compatibility rules as a public API.

    Marking the boundary explicitly makes that obligation visible in review
    rather than discovered when a consumer breaks.
    """

    name: ClassVar[str] = "integration.event"
