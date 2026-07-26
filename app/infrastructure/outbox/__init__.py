"""Transactional outbox.

Closes the gap between committing a change and publishing that it happened.
Without it, a crash in that window loses the event silently: the business
change is durable, nothing will ever act on it, and no error was raised.

``models`` defines the table; ``relay`` provides :func:`~relay.stage` for
producers and :class:`~relay.OutboxRelay` for the background publisher.

Use it for anything whose loss a customer would notice — payments, receipts,
provisioning. Skip it for cache invalidation, where the next write republishes
anyway and the extra insert is not worth it.
"""

from app.infrastructure.outbox.models import OutboxMessage
from app.infrastructure.outbox.relay import (
    OutboxRelay,
    pending_count,
    purge_published,
    reset_stalled,
    stage,
    stage_many,
)

__all__ = [
    "OutboxMessage",
    "OutboxRelay",
    "pending_count",
    "purge_published",
    "reset_stalled",
    "stage",
    "stage_many",
]
