"""The outbox table.

Why this file exists
--------------------
There is a gap between committing a transaction and publishing what happened.
Consider the ordinary shape of a write::

    async with session_scope() as session:
        await repo.add(invoice)  # (1) staged
        await session.commit()  # (2) durable
    await queue.enqueue(send_receipt)  # (3) published

A crash between (2) and (3) loses the receipt permanently: the invoice exists,
nothing will ever send the email, and no error was raised. Reordering does not
help — publishing before the commit means a worker can pick the job up and find
no invoice, or the transaction rolls back and the receipt is sent for something
that never happened.

The gap cannot be closed by care. Redis and PostgreSQL are separate systems, and
there is no transaction spanning both.

The outbox pattern
------------------
Write the *intent to publish* into the same database transaction as the business
change::

    async with session_scope() as session:
        await repo.add(invoice)
        await outbox.stage(session, event)  # same transaction
        await session.commit()  # both, or neither

A relay then reads unpublished rows and publishes them. Now the failure modes
are all survivable:

* Crash before commit → nothing happened at all.
* Crash after commit, before publish → the row is still there; the relay
  publishes it on the next pass.
* Crash after publish, before marking published → published twice, which is
  why every handler must be idempotent.

This converts "silently lost" into "at least once", which is the strongest
guarantee available without distributed transactions.

Cost
----
One extra insert per published event, and a relay process to run. Worth it for
anything whose loss is noticed by a customer — payments, receipts, provisioning
— and overkill for cache invalidation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base
from app.infrastructure.database.mixins import UUIDPrimaryKeyMixin


class OutboxMessage(Base, UUIDPrimaryKeyMixin):
    """One event awaiting publication.

    Deliberately **not** tenant-scoped. The relay publishes across every tenant,
    so a tenant filter would either hide rows from it or force it to iterate
    tenants. The tenant is recorded as a plain column for tracing and for
    restoring context when the message is handled.

    No foreign keys either: an outbox row must survive the deletion of whatever
    produced it, and a cascade would erase exactly the record needed to explain
    why something did or did not happen.
    """

    __tablename__ = "outbox_messages"

    #: Wire identifier, e.g. ``"billing.invoice_paid"``. Matched by consumers.
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Payload schema version, so a consumer can reject what it cannot read.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: The event body, exactly as :meth:`~app.events.base.DomainEvent.to_payload`
    #: produced it. JSONB rather than JSON so it can be indexed and queried when
    #: someone needs to find out what happened.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: Tenant the event belongs to, when it has one. Not a foreign key; see the
    #: class docstring.
    tenant_id: Mapped[UUID | None] = mapped_column(default=None)

    #: Correlation ID of the request that produced the event. This is what lets
    #: a support query follow one user action from the HTTP request, through the
    #: outbox, into the worker that acted on it.
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)

    #: When the fact became true. Distinct from ``created_at``: an event may be
    #: staged slightly after the thing it describes.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    #: Set once the relay has published successfully. ``NULL`` means pending —
    #: a nullable timestamp rather than a boolean, because "when" is the
    #: question actually asked during an incident.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    #: Publication attempts. Used for backoff and to spot a poison message.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Why the last attempt failed. Truncated on write; a stack trace here
    #: would make the table enormous for no diagnostic gain.
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    #: Earliest next attempt. Lets a failing message back off without blocking
    #: the messages queued behind it.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        # The relay's only query is "unpublished, due now, oldest first". A
        # partial index over just those rows stays small no matter how large the
        # table grows — published rows, which are the overwhelming majority,
        # are not in it at all.
        Index(
            "ix_outbox_messages_pending",
            "next_attempt_at",
            "created_at",
            postgresql_where=published_at.is_(None),
        ),
        # Supports the archival sweep that deletes old published rows.
        Index(
            "ix_outbox_messages_published_at",
            "published_at",
            postgresql_where=published_at.is_not(None),
        ),
    )

    @property
    def is_published(self) -> bool:
        """Whether this message has been published."""
        return self.published_at is not None
