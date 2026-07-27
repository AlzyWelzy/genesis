"""Tests for the repository bases, against a real PostgreSQL.

Why these exist
---------------
:class:`~app.infrastructure.database.repository.TenantRepository` is the single
mechanism preventing one customer from reading another's rows. When it
regresses, the symptom is a ``200 OK`` containing someone else's data — no
exception, no log line, nothing in the response to suggest anything happened.
No *feature* module could catch it either, because every one of them would be
built on the broken base.

So the weight here is on refusal: that a foreign row stays invisible to ``get``,
``get_many``, ``list``, ``count`` and ``exists`` alike, and that a write cannot
be steered at another tenant by supplying a ``tenant_id``.

Real PostgreSQL, never SQLite. The scoping is expressed as SQL, and the point is
to verify the SQL the database actually runs.

The models below are declared here rather than imported because Stage 1 has no
feature models by design. They live on the application's own ``Base`` so they
inherit the real naming convention and type mapping. Their ``_test_`` prefix is
what keeps them out of autogenerate — see ``_include_object`` in
``migrations/env.py`` — and their tables are created inside the test's
transaction, so they vanish with its rollback.
"""

from datetime import datetime
from uuid import UUID, uuid7

import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.pagination import PaginationParams
from app.core.context import tenant_id_var
from app.infrastructure.database.base import Base
from app.infrastructure.database.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.infrastructure.database.repository import (
    BaseRepository,
    SoftDeleteRepositoryMixin,
    TenantRepository,
)

pytestmark = pytest.mark.integration


class Widget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A tenant-scoped row.

    ``tenant_id`` is declared directly rather than through ``TenantMixin``,
    which carries a foreign key to ``tenants.id`` — a table Stage 2 creates. The
    column's *shape* is what the repository depends on, and that is identical.
    """

    __tablename__ = "_test_widgets"

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Gadget(Base, UUIDPrimaryKeyMixin, SoftDeleteMixin):
    """A globally scoped row that is soft-deleted rather than removed."""

    __tablename__ = "_test_gadgets"

    name: Mapped[str] = mapped_column(String(64), nullable=False)


class WidgetRepository(TenantRepository[Widget]):
    """Tenant-scoped access to widgets."""

    model = Widget


class GadgetRepository(BaseRepository[Gadget]):
    """Unscoped access to gadgets, soft-deleted rows included."""

    model = Gadget


class LiveGadgetRepository(SoftDeleteRepositoryMixin, BaseRepository[Gadget]):
    """Gadget access excluding soft-deleted rows."""

    model = Gadget


TENANT_A = UUID("aaaaaaaa-0000-7000-8000-000000000001")
TENANT_B = UUID("bbbbbbbb-0000-7000-8000-000000000002")


@pytest.fixture
async def tables(session) -> None:
    """Create the throwaway tables inside the test's own transaction.

    PostgreSQL makes DDL transactional, so the rollback in the ``session``
    fixture drops them again. No cleanup code, and nothing survives a crashed
    test to poison the next one.
    """
    connection = await session.connection()
    # Selected out of the metadata by name rather than through
    # ``Model.__table__``, which the declarative API types as the broader
    # ``FromClause``.
    wanted = {Widget.__tablename__, Gadget.__tablename__}
    targets = [table for table in Base.metadata.sorted_tables if table.name in wanted]
    await connection.run_sync(
        lambda sync_connection: Base.metadata.create_all(
            sync_connection, tables=targets
        )
    )


@pytest.fixture
def as_tenant_a():
    """Enter tenant A's context, and leave it cleanly afterwards."""
    token = tenant_id_var.set(TENANT_A)
    yield TENANT_A
    tenant_id_var.reset(token)


@pytest.fixture
async def widgets(session, tables, as_tenant_a) -> WidgetRepository:
    """A widget repository over one row for each of two tenants."""
    session.add_all(
        [
            Widget(tenant_id=TENANT_A, name="mine"),
            Widget(tenant_id=TENANT_B, name="theirs"),
        ]
    )
    await session.flush()
    return WidgetRepository(session)


async def _foreign_widget_id(session) -> UUID:
    """The ID of the row belonging to the *other* tenant."""
    result = await session.execute(
        Widget.__table__.select().where(Widget.tenant_id == TENANT_B)
    )
    return result.one().id


class TestTenantScoping:
    """The load-bearing behaviour. Each of these is a data leak if it fails."""

    async def test_list_excludes_another_tenants_rows(self, widgets) -> None:
        rows, total = await widgets.list(params=PaginationParams(page=1, size=50))
        assert [row.name for row in rows] == ["mine"]
        assert total == 1

    async def test_get_by_id_cannot_reach_another_tenants_row(
        self, widgets, session
    ) -> None:
        """The dangerous case: the caller already holds the ID.

        A primary-key lookup is exactly where ``session.get()`` would be the
        obvious implementation, and exactly where it would leak — it consults
        the identity map and the PK index, and no scoping applies to either.
        """
        assert await widgets.get(await _foreign_widget_id(session)) is None

    async def test_get_many_filters_foreign_ids_out(self, widgets, session) -> None:
        """A batch fetch must not become the way around the single fetch."""
        result = await session.execute(Widget.__table__.select())
        ids = [row.id for row in result.all()]
        assert len(ids) == 2

        found = await widgets.get_many(ids)
        assert [row.name for row in found] == ["mine"]

    async def test_exists_does_not_confirm_a_foreign_row(
        self, widgets, session
    ) -> None:
        """Existence is privileged: confirming an ID confirms another tenant's data."""
        assert await widgets.exists(await _foreign_widget_id(session)) is False

    async def test_count_counts_only_this_tenant(self, widgets) -> None:
        assert await widgets.count() == 1


class TestTenantStamping:
    async def test_add_stamps_the_current_tenant(self, session, tables, as_tenant_a):
        widget = await WidgetRepository(session).add(Widget(name="new"))
        assert widget.tenant_id == TENANT_A

    async def test_add_overrides_a_caller_supplied_tenant(
        self, session, tables, as_tenant_a
    ) -> None:
        """A ``tenant_id`` deserialised from a request body is a cross-tenant write.

        Stamping centrally is what makes the field impossible to influence from
        outside, so it must *overwrite* whatever is already set rather than
        defer to it.
        """
        repo = WidgetRepository(session)
        widget = await repo.add(Widget(tenant_id=TENANT_B, name="smuggled"))
        assert widget.tenant_id == TENANT_A

    async def test_add_all_stamps_every_row(self, session, tables, as_tenant_a):
        rows = await WidgetRepository(session).add_all(
            [Widget(name="a"), Widget(tenant_id=TENANT_B, name="b")]
        )
        assert {row.tenant_id for row in rows} == {TENANT_A}

    async def test_a_missing_tenant_raises_rather_than_reading_everything(
        self, session, tables
    ) -> None:
        """No context must be a loud failure, never an unscoped query."""
        with pytest.raises(RuntimeError):
            await WidgetRepository(session).count()


class TestSoftDelete:
    @pytest.fixture
    async def gadget(self, session, tables) -> Gadget:
        row = Gadget(name="doomed")
        session.add(row)
        await session.flush()
        return row

    async def test_delete_marks_rather_than_removes(self, session, gadget) -> None:
        await GadgetRepository(session).delete(gadget)
        assert gadget.deleted_at is not None

        remaining = await session.execute(Gadget.__table__.select())
        assert len(remaining.all()) == 1

    async def test_a_soft_deleted_row_is_invisible_to_the_filtered_repository(
        self, session, gadget
    ) -> None:
        await GadgetRepository(session).delete(gadget)

        assert await LiveGadgetRepository(session).get(gadget.id) is None
        assert await LiveGadgetRepository(session).count() == 0

    async def test_the_escape_hatch_still_sees_it(self, session, gadget) -> None:
        """Restore flows and admin tooling need it; the default must not."""
        repo = LiveGadgetRepository(session)
        await GadgetRepository(session).delete(gadget)

        result = await session.execute(repo.select_including_deleted())
        assert len(result.scalars().all()) == 1

    async def test_a_hard_delete_model_is_actually_removed(
        self, session, tables, as_tenant_a
    ) -> None:
        """``delete`` must do the right thing for both kinds of model."""
        repo = WidgetRepository(session)
        widget = await repo.add(Widget(name="gone"))
        await repo.delete(widget)

        assert await repo.count() == 0


class TestListing:
    async def test_the_total_reflects_the_filter_not_the_table(
        self, session, tables, as_tenant_a
    ) -> None:
        """A total counted over the table reports a page count that is a lie."""
        repo = WidgetRepository(session)
        await repo.add_all([Widget(name=f"w{i}") for i in range(5)])

        statement = repo.select().where(Widget.name.in_(["w0", "w1"]))
        rows, total = await repo.list(
            params=PaginationParams(page=1, size=50), statement=statement
        )
        assert len(rows) == 2
        assert total == 2

    async def test_pagination_returns_the_requested_slice(
        self, session, tables, as_tenant_a
    ) -> None:
        repo = WidgetRepository(session)
        await repo.add_all([Widget(name=f"w{i}") for i in range(5)])

        rows, total = await repo.list(
            params=PaginationParams(page=2, size=2), order_by=[Widget.name]
        )
        assert [row.name for row in rows] == ["w2", "w3"]
        assert total == 5

    async def test_the_count_is_taken_before_the_limit(
        self, session, tables, as_tenant_a
    ) -> None:
        """Otherwise the total equals the page size and pagination never ends."""
        repo = WidgetRepository(session)
        await repo.add_all([Widget(name=f"w{i}") for i in range(5)])

        _, total = await repo.list(params=PaginationParams(page=1, size=2))
        assert total == 5

    async def test_a_page_past_the_end_is_empty_not_an_error(
        self, session, tables, as_tenant_a
    ) -> None:
        repo = WidgetRepository(session)
        await repo.add(Widget(name="only"))

        rows, total = await repo.list(params=PaginationParams(page=9, size=10))
        assert rows == []
        assert total == 1


class TestEmptyInputs:
    async def test_get_many_with_no_ids_issues_no_query(
        self, session, tables, as_tenant_a
    ) -> None:
        """``IN ()`` is a syntax error in PostgreSQL, so this must short-circuit."""
        assert await WidgetRepository(session).get_many([]) == []

    async def test_add_all_with_no_entities_is_a_no_op(
        self, session, tables, as_tenant_a
    ) -> None:
        assert await WidgetRepository(session).add_all([]) == []


class TestFlushBehaviour:
    async def test_add_populates_server_side_defaults(
        self, session, tables, as_tenant_a
    ) -> None:
        """Flushing is what makes the generated values available to the caller."""
        widget = await WidgetRepository(session).add(Widget(name="w"))
        assert isinstance(widget.created_at, datetime)

    async def test_add_does_not_commit(self, session, tables, as_tenant_a) -> None:
        """Commit belongs to the service: only it knows what is one operation."""
        await WidgetRepository(session).add(Widget(name="w"))
        assert session.in_transaction()

    def test_the_id_is_available_before_the_insert(self) -> None:
        """The property that makes one-pass writes possible.

        A caller can reference the row — in an event, in a related record — in
        the same pass, before anything has been flushed.
        """
        assert isinstance(Widget(name="w").id, UUID)

    def test_two_instances_get_different_ids(self) -> None:
        assert Widget(name="a").id != Widget(name="b").id

    def test_an_explicit_id_is_not_overwritten(self) -> None:
        """Restores and cross-environment imports supply their own."""
        pinned = uuid7()
        assert Widget(id=pinned, name="w").id == pinned


class TestRepr:
    def test_repr_shows_only_the_primary_key(self) -> None:
        """Reprs reach logs and tracebacks; column values may be secrets."""
        widget = Widget(name="super-secret-name")
        assert "super-secret-name" not in repr(widget)
        assert "Widget" in repr(widget)
