"""Async engine, session factory and the request-scoped session dependency.

Why this file exists
--------------------
The engine owns a connection pool and must exist exactly once per process; a
session is cheap, stateful and must exist exactly once per unit of work. Getting
that boundary wrong is the most expensive mistake in a SQLAlchemy codebase —
a shared session leaks state across requests, and a per-query session destroys
the pool.

This module defines both lifetimes and exposes the only two ways to obtain a
session:

* :func:`get_session` — a FastAPI dependency giving a router (and, through it,
  a service) one session for the whole request.
* :func:`session_scope` — a context manager for code with no request: workers,
  CLI scripts, scheduled jobs.

Transaction policy
------------------
The session is committed by the *caller of the service*, not by repositories.
Repositories add, delete and flush; they never commit. That keeps a service free
to perform several repository operations in one atomic transaction, which is
impossible if each repository commits on its own.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

#: Process-wide engine and connection pool. Disposed in the app lifespan.
engine: AsyncEngine = create_async_engine(
    str(settings.database.url),
    echo=settings.database.echo,
    pool_size=settings.database.pool_size,
    max_overflow=settings.database.max_overflow,
    pool_timeout=settings.database.pool_timeout,
    pool_recycle=settings.database.pool_recycle,
    pool_pre_ping=settings.database.pool_pre_ping,
)

#: Session factory.
#:
#: ``expire_on_commit=False`` matters for async code: with the default, touching
#: any attribute after a commit triggers a lazy refresh, which raises
#: ``MissingGreenlet`` outside an await context. Keeping objects usable after
#: commit is what lets a router serialise what a service returned.
session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    One session per request, closed when the response is finished. Nothing is
    committed here: a request that raises must not persist partial writes, and
    a request that succeeds commits explicitly in the service layer.

    Yields:
        The session bound to this request.
    """
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager providing a session outside of a request.

    For queue consumers, scheduled jobs and scripts, where no dependency
    injection is available. Commits on success, rolls back on failure —
    appropriate because the caller *is* the unit of work here.

    Yields:
        A session wrapping a single transaction.
    """
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called from the application lifespan."""
    await engine.dispose()


# TODO: add a `get_read_session` dependency bound to a read-replica engine once
# read/write splitting is introduced.
# TODO: consider a `SessionTransaction` dependency that opens an explicit
# `session.begin()` block when a strict one-transaction-per-request policy is
# preferred over service-managed commits.
