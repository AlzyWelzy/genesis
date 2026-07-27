"""Reusable model mixins.

Why this file exists
--------------------
Almost every table needs a surrogate primary key, audit timestamps and a tenant
column. Copying those into two hundred models guarantees drift: one will use a
naive timestamp, another will forget ``onupdate``, a third will omit the tenant
index and turn every query into a sequential scan.

Mixins let a model declare intent by inheritance while the definitions live in
one reviewed place::

    class Invoice(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
        __tablename__ = "invoices"

Design rules
------------
* A mixin adds columns or column-level behaviour only. *Query* behaviour — the
  tenant filter, the soft-delete filter — lives in the repository base, where
  it is applied uniformly and cannot be forgotten per call site.
* Timestamps are set by the **database** (``server_default``/``onupdate``), not
  by Python, so rows written by a migration or by hand are also correct.
"""

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid7

from sqlalchemy import DateTime, ForeignKey, Index, Integer, event, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class UUIDPrimaryKeyMixin:
    """Adds a UUIDv7 surrogate primary key.

    Why UUID over auto-increment: identifiers can be minted client-side, they
    do not leak row counts or growth rate, ``/users/1`` cannot be walked to
    ``/users/2``, and rows survive being merged across environments or shards.

    Why **v7** over v4: a v7 UUID embeds a millisecond timestamp in its high
    bits, so freshly generated keys sort together and inserts land at the right
    edge of the B-tree. Random v4 keys scatter writes across the whole index,
    which on a large table means constant page splits and a cache hit rate that
    collapses. v7 keeps v4's opacity and recovers most of a sequential key's
    write locality.

    Generated in Python (stdlib ``uuid.uuid7``, 3.14+) rather than by the
    database, so the application knows the ID before the ``INSERT`` — which is
    what lets it build related rows and publish events in one pass without a
    round trip.

    That last property is why the ID is assigned at **construction** and not
    only at flush. A bare ``default=uuid7`` is an *insert* default: SQLAlchemy
    evaluates it while flushing, so ``Invoice().id`` is ``None`` until then, and
    code doing exactly what this docstring advertises::

        invoice = Invoice(...)
        await stage(session, InvoicePaid(invoice_id=invoice.id))
        await repo.add(invoice)

    stages an event carrying ``None`` — silently, because the column is
    perfectly happy to be populated a moment later. The listener below closes
    that window. ``default=uuid7`` stays as the backstop for rows created by
    paths that bypass ``__init__``, such as a bulk ``insert()``.
    """

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)


@event.listens_for(UUIDPrimaryKeyMixin, "init", propagate=True)
def _assign_primary_key_on_init(
    _target: object, _args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Populate ``id`` when the instance is constructed, not when it is flushed.

    ``propagate=True`` is what makes a single listener on the unmapped mixin
    apply to every model inheriting it, including ones declared later.

    ``setdefault`` rather than assignment: an explicitly supplied ID — restoring
    a record, importing across environments, a test pinning a value — must win.
    """
    kwargs.setdefault("id", uuid7())


class TimestampMixin:
    """Adds database-managed ``created_at`` / ``updated_at`` audit columns.

    Both are timezone-aware and defaulted server-side, so every write path —
    ORM, raw SQL, migration — produces consistent values.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Adds the tenant discriminator that isolates rows between customers.

    This is the load-bearing column of a row-level multi-tenant system. Every
    query against a table carrying it **must** filter on it; a single missed
    filter returns another customer's data with a 200 status and nothing in the
    response to indicate it. That is why the filter is applied centrally in
    :class:`~app.infrastructure.database.repository.TenantRepository` rather
    than written per query.

    The index is not optional. Effectively every query begins with
    ``WHERE tenant_id = ?``, so without it every query is a sequential scan of
    every tenant's rows.

    Composite indexes on ``(tenant_id, <frequently_filtered_column>)`` should be
    declared per model — a tenant-only index still leaves the database sifting
    through one tenant's entire history.
    """

    @declared_attr
    @classmethod
    def tenant_id(cls) -> Mapped[UUID]:
        """Owning tenant. Indexed, non-nullable, and enforced by a foreign key."""
        return mapped_column(
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class SoftDeleteMixin:
    """Adds a nullable ``deleted_at`` marking logical deletion.

    A row with ``deleted_at IS NOT NULL`` must be treated as absent by every
    read path. A model cannot enforce that, and relying on developers to
    remember the filter at each call site is exactly how deleted records
    reappear in an export — so the repository applies it.

    Soft deletion is not free: unique constraints must become partial
    (``WHERE deleted_at IS NULL``), or a user cannot re-register an email they
    deleted. Apply this mixin deliberately, not by default.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        index=True,
    )

    @property
    def is_deleted(self) -> bool:
        """Whether the row is logically deleted."""
        return self.deleted_at is not None


class AuditMixin:
    """Adds ``created_by`` / ``updated_by`` actor columns.

    Answers "who changed this", which timestamps alone cannot. Deliberately
    *not* foreign keys to ``users``: an audit trail must survive the deletion of
    the user it names, and a ``CASCADE`` would erase precisely the record an
    investigation needs.

    This is row-level attribution, not an audit *log*. A full log — what
    changed, from what, to what — is a Stage 2 platform feature.
    """

    created_by: Mapped[UUID | None] = mapped_column(default=None)
    updated_by: Mapped[UUID | None] = mapped_column(default=None, onupdate=None)


class VersionMixin:
    """Adds an optimistic-locking version counter.

    For records where two concurrent edits must not silently overwrite one
    another. SQLAlchemy increments the column on each flush and raises
    ``StaleDataError`` if the row changed underneath, which the service
    translates into a :class:`~app.core.exceptions.ConflictError` so the client
    can refetch and retry.

    Cheaper than row locks and correct across HTTP request boundaries, where a
    lock cannot be held.
    """

    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: ClassVar[dict[str, Any]] = {"version_id_col": version_id}


def tenant_index(table_name: str, *columns: str) -> Index:
    """Build a composite index prefixed with ``tenant_id``.

    Column order in a composite index is not cosmetic: PostgreSQL can only use
    a leading prefix of the columns, so ``(tenant_id, status)`` serves both
    "this tenant" and "this tenant's active rows", while ``(status, tenant_id)``
    serves neither well. Since every query starts with the tenant, it must lead.

    Args:
        table_name: Table the index belongs to, used to build its name.
        *columns: Additional columns after ``tenant_id``.

    Returns:
        The index, for use in a model's ``__table_args__``.
    """
    suffix = "_".join(columns)
    return Index(f"ix_{table_name}_tenant_{suffix}", "tenant_id", *columns)
