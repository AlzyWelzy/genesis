"""Two transactions racing for the same row.

Why this matters
----------------
Optimistic locking and unique constraints exist *only* for the concurrent case.
Run the operations one after another and the code behaves identically whether the
version column is wired up or not, so a sequential test proves nothing about the
guarantee it appears to be testing.

Genuinely separate sessions throughout. Two "concurrent" writes on one session
are serialised by that session and would pass on code that silently overwrites.
"""

import asyncio

import pytest
from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from app.infrastructure.database.base import Base
from app.infrastructure.database.mixins import UUIDPrimaryKeyMixin, VersionMixin
from app.infrastructure.database.session import session_factory, session_scope

pytestmark = pytest.mark.integration


class Ledger(Base, UUIDPrimaryKeyMixin, VersionMixin):
    """A versioned row with a unique business key."""

    __tablename__ = "_test_ledger"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    balance: Mapped[int] = mapped_column(default=0)


@pytest.fixture
async def ledger_table(shared_engine):
    """Create and drop the table around the test, committed.

    Committed rather than transaction-scoped: separate sessions cannot see a
    table created inside another session's uncommitted transaction, and separate
    sessions are the entire point here.
    """
    from app.infrastructure.database.session import engine

    # Selected out of the metadata by name rather than via ``Model.__table__``,
    # which the declarative API types as the broader ``FromClause``.
    tables = [t for t in Base.metadata.sorted_tables if t.name == Ledger.__tablename__]

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tables)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all, tables=tables)


class TestOptimisticLocking:
    async def test_the_second_writer_is_told_to_refetch(self, ledger_table) -> None:
        """Lost-update prevention, which is what the version column is for.

        Both writers read the same version, both write. Without the check the
        second silently overwrites the first and the money is simply gone.
        """
        async with session_scope() as session:
            session.add(Ledger(code="acct", balance=100))

        async def credit(amount: int) -> str:
            async with session_factory() as session:
                row = (
                    await session.execute(select(Ledger).where(Ledger.code == "acct"))
                ).scalar_one()
                # Both readers now hold version 1. Yield so the other gets here
                # before either writes.
                await asyncio.sleep(0.05)
                row.balance += amount
                try:
                    await session.commit()
                except StaleDataError:
                    return "refused"
                return "applied"

        outcomes = await asyncio.gather(credit(10), credit(20), return_exceptions=True)
        results = [o for o in outcomes if isinstance(o, str)]

        assert "applied" in results
        assert "refused" in results or len(results) < 2

    async def test_no_update_is_silently_lost(self, ledger_table) -> None:
        """The consequence stated in terms of the data, not the exception."""
        async with session_scope() as session:
            session.add(Ledger(code="acct2", balance=0))

        async def bump() -> None:
            async with session_factory() as session:
                row = (
                    await session.execute(select(Ledger).where(Ledger.code == "acct2"))
                ).scalar_one()
                await asyncio.sleep(0.05)
                row.balance += 1
                await session.commit()

        await asyncio.gather(bump(), bump(), return_exceptions=True)

        async with session_scope() as session:
            row = (
                await session.execute(select(Ledger).where(Ledger.code == "acct2"))
            ).scalar_one()

        # Either both applied (2) or one was refused (1). Never 2 writers
        # believing they succeeded while only one increment landed.
        assert row.balance in (1, 2)
        assert row.version_id == row.balance + 1


class TestUniqueConstraintRace:
    async def test_only_one_concurrent_insert_wins(self, ledger_table) -> None:
        """Two registrations for one email is the ordinary case, not an edge.

        Checking before inserting cannot close this: between any ``SELECT`` and
        its ``INSERT`` there is a window, and the constraint is what closes it.
        """

        async def insert() -> str:
            async with session_factory() as session:
                session.add(Ledger(code="duplicate", balance=0))
                try:
                    await session.commit()
                except IntegrityError:
                    return "rejected"
                return "inserted"

        outcomes = await asyncio.gather(*(insert() for _ in range(8)))

        assert outcomes.count("inserted") == 1
        assert outcomes.count("rejected") == 7

    async def test_exactly_one_row_exists_afterwards(self, ledger_table) -> None:
        async def insert() -> None:
            async with session_factory() as session:
                session.add(Ledger(code="once", balance=0))
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()

        await asyncio.gather(*(insert() for _ in range(8)))

        async with session_scope() as session:
            rows = (
                (await session.execute(select(Ledger).where(Ledger.code == "once")))
                .scalars()
                .all()
            )

        assert len(rows) == 1
