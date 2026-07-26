"""Repository base classes.

Why this file exists
--------------------
In a row-level multi-tenant system, one forgotten ``WHERE tenant_id = ?``
returns another customer's data with a 200 status. Nothing in the response, the
logs or the tests says anything is wrong. It is the highest-severity bug this
architecture can produce and the easiest one to write.

A convention ("always filter by tenant") does not prevent it, because
conventions are enforced by attention and there will be thousands of queries.
:class:`TenantRepository` makes the filter structural instead: every query is
built through :meth:`~BaseRepository.select`, which applies the tenant predicate
before the caller sees the statement. Escaping it requires writing ``select()``
by hand — visible in review, and greppable.

The same argument applies to soft deletion: a filter every reader must remember
is a filter someone will forget.

Layer contract
--------------
Repositories translate between the database and domain objects. They build
statements, execute them, apply eager loading, pagination, sorting and scoping,
and ``flush()`` when the caller needs a generated ID.

They never: apply business rules, call other services, publish events, or
``commit()``. Commit belongs to the service, because only the service knows
whether three writes are one atomic operation.

See ``docs/architecture/database-conventions.md``.
"""

from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import PaginationParams
from app.common.utils.datetime import utc_now
from app.core.context import require_tenant_id
from app.infrastructure.database.base import Base


class BaseRepository[ModelT: Base]:
    """Data access for a single model, without tenant scoping.

    Use directly only for genuinely global tables — tenants themselves, feature
    flags, system configuration. Anything holding customer data must use
    :class:`TenantRepository`.

    Args:
        session: The request-scoped session. Injected, never created here: the
            repository must join whatever transaction its caller opened, and a
            repository that makes its own session cannot participate in one.
    """

    #: The mapped class this repository reads and writes. Set by every subclass.
    model: ClassVar[Any]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- Query construction --------------------------------------------------

    def select(self) -> Select[tuple[ModelT]]:
        """Start a query for this repository's model.

        The single entry point for building statements. Subclasses override it
        to layer in scoping, so every query inherits it automatically.
        """
        return select(self.model)

    def _apply_pagination(
        self, statement: Select[tuple[ModelT]], params: PaginationParams
    ) -> Select[tuple[ModelT]]:
        """Apply ``LIMIT``/``OFFSET`` for a page."""
        return statement.limit(params.limit).offset(params.offset)

    # -- Reads ---------------------------------------------------------------

    async def get(self, entity_id: UUID) -> ModelT | None:
        """Fetch one row by primary key, or ``None``.

        Returns ``None`` rather than raising: "not found" is a normal outcome
        at this layer, and whether it is an error depends on the caller. The
        service decides to raise :class:`~app.core.exceptions.NotFoundError`.

        Goes through :meth:`select` rather than ``session.get()`` so tenant and
        soft-delete scoping still apply — ``session.get()`` bypasses both, which
        would make it a cross-tenant read by primary key.
        """
        result = await self.session.execute(
            self.select().where(self.model.id == entity_id)
        )
        return result.scalar_one_or_none()

    async def get_many(self, entity_ids: Sequence[UUID]) -> Sequence[ModelT]:
        """Fetch several rows by primary key in one statement.

        For resolving a batch of references without an N+1 loop. Missing IDs
        are simply absent from the result; the caller compares lengths if that
        matters.
        """
        if not entity_ids:
            return []
        result = await self.session.execute(
            self.select().where(self.model.id.in_(entity_ids))
        )
        return result.scalars().all()

    async def list(
        self,
        *,
        params: PaginationParams,
        statement: Select[tuple[ModelT]] | None = None,
        order_by: Sequence[Any] | None = None,
    ) -> tuple[Sequence[ModelT], int]:
        """Fetch one page of rows plus the total count.

        Returns both because a paginated response needs the total, and running
        the count as a separate hand-written query is how the two drift apart
        under a filter.

        The count is over the *filtered* set, not the table: both queries share
        one predicate. On very large tables an exact count is itself expensive —
        that is the point at which cursor pagination should replace this method
        rather than the count being optimised.

        Args:
            params: Page number and size.
            statement: A pre-filtered statement from :meth:`select`. Defaults to
                the unfiltered (but still scoped) query.
            order_by: Order-by clauses. Should always include a unique
                tiebreaker — see :func:`app.common.sorting.build_order_by` —
                or rows will duplicate and skip between pages.

        Returns:
            A ``(rows, total)`` pair.
        """
        stmt = self.select() if statement is None else statement
        total = await self.count(stmt)

        if order_by:
            stmt = stmt.order_by(*order_by)

        result = await self.session.execute(self._apply_pagination(stmt, params))
        return result.scalars().all(), total

    async def exists(self, entity_id: UUID) -> bool:
        """Whether a row with this ID exists within the current scope.

        Issues ``SELECT EXISTS (...)`` rather than loading the row: fetching a
        full entity — with its columns and any eager loads — to answer a boolean
        is wasted I/O on a hot path.
        """
        scoped = self.select().where(self.model.id == entity_id)
        result = await self.session.execute(select(scoped.exists()))
        return bool(result.scalar_one())

    async def count(self, statement: Select[Any] | None = None) -> int:
        """Count rows matching a statement, defaulting to the full scope.

        Wraps the statement in a subquery so any ``LIMIT``, ``ORDER BY`` or
        join in it is respected rather than silently dropped.
        """
        stmt = self.select() if statement is None else statement
        result = await self.session.execute(
            select(func.count()).select_from(stmt.subquery())
        )
        return result.scalar_one()

    # -- Writes --------------------------------------------------------------

    async def add(self, entity: ModelT) -> ModelT:
        """Stage a new row and flush it.

        Flushes but does **not** commit. The flush surfaces constraint
        violations now, while there is context to interpret them, and populates
        server-side defaults; the transaction stays open for the service to
        close.
        """
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        """Stage several rows and flush once.

        One flush rather than one per entity: each flush is a round trip, and
        for a bulk import that difference is the whole runtime.

        Large batches should be chunked by the caller — see
        :func:`app.common.utils.collections.chunk` — so a single statement
        cannot exceed PostgreSQL's bind-parameter limit.
        """
        if not entities:
            return []
        self.session.add_all(list(entities))
        await self.session.flush()
        return entities

    async def delete(self, entity: ModelT) -> None:
        """Delete a row.

        Models carrying :class:`~app.infrastructure.database.mixins.SoftDeleteMixin`
        are marked rather than removed, so a single call site does the right
        thing for both kinds of model.
        """
        if hasattr(entity, "deleted_at"):
            entity.deleted_at = utc_now()
            await self.session.flush()
            return
        await self.session.delete(entity)
        await self.session.flush()


class TenantRepository[ModelT: Base](BaseRepository[ModelT]):
    """Data access for a tenant-scoped model.

    Overrides :meth:`select` to apply the tenant predicate to *every* query
    built through it. That is the entire point of the class: correctness by
    construction rather than by discipline.

    The tenant comes from :mod:`app.core.context`, not from a method argument.
    A parameter can be passed wrongly — or passed straight from user input —
    whereas the context is populated once, at the edge, by an authenticated
    dependency. :func:`~app.core.context.require_tenant_id` raises rather than
    returning ``None``, so a missing tenant is a 500 instead of a silent
    cross-tenant read.
    """

    def select(self) -> Select[tuple[ModelT]]:
        """Start a query scoped to the current tenant."""
        return super().select().where(self.model.tenant_id == require_tenant_id())

    async def add(self, entity: ModelT) -> ModelT:
        """Stage a new row, stamping it with the current tenant.

        Set here rather than trusting the caller: a client-supplied
        ``tenant_id`` in a request body is a cross-tenant write waiting to
        happen, and stamping it centrally makes the field impossible to
        influence from outside.
        """
        entity.tenant_id = require_tenant_id()
        return await super().add(entity)

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        """Stage several rows, stamping each with the current tenant."""
        tenant_id = require_tenant_id()
        for entity in entities:
            entity.tenant_id = tenant_id
        return await super().add_all(entities)


class SoftDeleteRepositoryMixin:
    """Excludes soft-deleted rows from every query.

    Mixed in *before* the repository base so its :meth:`select` wraps the
    scoped one::

        class InvoiceRepository(
            SoftDeleteRepositoryMixin, TenantRepository[Invoice]
        ):
            model = Invoice

    The escape hatch is explicit and named, so restoring a record or building
    an admin view is possible without making the default unsafe.
    """

    model: ClassVar[Any]

    def select(self) -> Select[Any]:
        """Start a query excluding soft-deleted rows."""
        return super().select().where(self.model.deleted_at.is_(None))

    def select_including_deleted(self) -> Select[Any]:
        """Start a query that includes soft-deleted rows.

        For restore flows and admin tooling only. Named rather than a boolean
        flag so every use is greppable.
        """
        return super().select()
