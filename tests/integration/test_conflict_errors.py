"""Database conflicts reaching the client as conflicts, not as server faults.

Why these exist
---------------
``ConflictError``'s docstring says it is "typically raised when a unique
constraint fires or an optimistic-lock version check fails", and ``VersionMixin``
says a ``StaleDataError`` is "translated into a ``ConflictError`` so the client
can refetch and retry". Neither translation existed. Both exceptions fell
through to the catch-all handler, so:

* two people editing one record produced a **500**, an alert and a traceback,
  where the honest answer is 409 "refetch and retry";
* two concurrent registrations for one email produced a **500**, where the
  loser's request was perfectly well-formed.

Every Stage 2 feature hits the second case on its first uniqueness constraint,
so this would have surfaced immediately — as an outage-shaped alert for ordinary
user behaviour.

Real PostgreSQL, because the thing being translated is a real SQLSTATE. A
mocked exception would only prove the handler matches what the test invented.
"""

from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from app.infrastructure.database.base import Base
from app.infrastructure.database.mixins import UUIDPrimaryKeyMixin, VersionMixin

pytestmark = [
    pytest.mark.integration,
    # A flush that raises invalidates the transaction it ran in. The `session`
    # fixture then rolls back its own outer transaction over a connection
    # SQLAlchemy has already deassociated, and warns. It is an artefact of
    # deliberately provoking database errors inside a rollback-per-test
    # fixture, not a defect in either — suppressed here, narrowly, rather than
    # left to add noise every run.
    pytest.mark.filterwarnings(
        "ignore:transaction already deassociated from connection"
    ),
]


class Gizmo(Base, UUIDPrimaryKeyMixin, VersionMixin):
    """A versioned row with a unique column."""

    __tablename__ = "_test_gizmos"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False, default="")


@pytest.fixture
async def tables(session) -> None:
    """Create the throwaway table inside the test's own transaction."""
    connection = await session.connection()
    targets = [t for t in Base.metadata.sorted_tables if t.name == Gizmo.__tablename__]
    await connection.run_sync(
        lambda sync_connection: Base.metadata.create_all(
            sync_connection, tables=targets
        )
    )


class TestOptimisticLocking:
    """``VersionMixin`` itself, which had no test at all."""

    async def test_the_version_starts_at_one(self, session, tables) -> None:
        gizmo = Gizmo(code="a")
        session.add(gizmo)
        await session.flush()

        assert gizmo.version_id == 1

    async def test_the_version_increments_on_update(self, session, tables) -> None:
        gizmo = Gizmo(code="a")
        session.add(gizmo)
        await session.flush()

        gizmo.label = "changed"
        await session.flush()

        assert gizmo.version_id == 2

    async def test_a_concurrent_edit_raises_stale_data(self, session, tables) -> None:
        """The whole point: the second writer must not silently win.

        Simulated by changing the row out from under the ORM, which is exactly
        what a second transaction would have done.
        """
        from sqlalchemy import update

        gizmo = Gizmo(code="a")
        session.add(gizmo)
        await session.flush()

        # Another writer commits first, bumping the version. Issued against
        # the ORM class rather than ``__table__``, which the declarative API
        # types as the broader ``FromClause``.
        await session.execute(
            update(Gizmo)
            .where(Gizmo.id == gizmo.id)
            .values(version_id=99, label="theirs")
            .execution_options(synchronize_session=False)
        )

        # Inside a SAVEPOINT: a failed flush poisons the transaction it runs in,
        # and the fixture's outer transaction has to survive to roll the test
        # back. Without the savepoint the teardown warns about a transaction
        # already deassociated from its connection.
        gizmo.label = "mine"
        with pytest.raises(StaleDataError):
            async with session.begin_nested():
                await session.flush()

    async def test_two_models_get_their_own_version_column(self) -> None:
        """A mixin-declared ``__mapper_args__`` must resolve per subclass.

        If it bound the mixin's own column object, every model sharing the mixin
        would version against one table's column — which fails confusingly at
        mapper configuration rather than obviously here.
        """
        assert Gizmo.__mapper__.version_id_col is Gizmo.__table__.c.version_id


class TestUniqueViolation:
    async def test_a_duplicate_raises_integrity_error(self, session, tables) -> None:
        session.add(Gizmo(code="dup"))
        await session.flush()

        session.add(Gizmo(code="dup"))
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.flush()

    async def test_the_sqlstate_identifies_it_as_a_unique_violation(
        self, session, tables
    ) -> None:
        """The handler branches on this code, so the code itself is the contract."""
        from app.core.exceptions import _is_unique_violation

        session.add(Gizmo(code="dup"))
        await session.flush()

        session.add(Gizmo(code="dup"))
        with pytest.raises(IntegrityError) as exc:
            async with session.begin_nested():
                await session.flush()

        assert _is_unique_violation(exc.value) is True


class TestHandlerTranslation:
    """Through the real HTTP stack, since that is where the translation lives."""

    @pytest.fixture
    def failing_app(self, app):
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        class FakeUniqueViolationError(Exception):
            sqlstate = "23505"

        class FakeCheckViolationError(Exception):
            sqlstate = "23514"

        @app.get("/_t/stale")
        async def stale() -> dict:
            raise StaleDataError("row changed")

        @app.get("/_t/duplicate")
        async def duplicate() -> dict:
            raise SAIntegrityError("INSERT", {}, FakeUniqueViolationError())

        @app.get("/_t/bad-constraint")
        async def bad_constraint() -> dict:
            raise SAIntegrityError("INSERT", {}, FakeCheckViolationError())

        return app

    async def test_stale_data_becomes_409(self, failing_app, permissive_client) -> None:
        """A 500 tells the user the server broke; the truth is "refetch"."""
        response = await permissive_client.get("/_t/stale")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_data"

    async def test_a_unique_violation_becomes_409(
        self, failing_app, permissive_client
    ) -> None:
        response = await permissive_client.get("/_t/duplicate")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "already_exists"

    async def test_the_database_message_is_never_forwarded(
        self, failing_app, permissive_client
    ) -> None:
        """It names tables, columns and constraints — an internal schema dump."""
        body = (await permissive_client.get("/_t/duplicate")).text

        assert "INSERT" not in body
        assert "_test_gizmos" not in body

    async def test_a_non_unique_constraint_stays_a_500(
        self, failing_app, permissive_client
    ) -> None:
        """A check or foreign-key failure means the app admitted bad data.

        That is a bug, and reporting it to the user as a routine conflict is how
        it goes unnoticed.
        """
        response = await permissive_client.get("/_t/bad-constraint")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"

    async def test_the_500_still_carries_a_request_id(
        self, failing_app, permissive_client
    ) -> None:
        body = (await permissive_client.get("/_t/bad-constraint")).json()

        assert body["error"]["request_id"]
        UUID(hex=body["error"]["request_id"])
