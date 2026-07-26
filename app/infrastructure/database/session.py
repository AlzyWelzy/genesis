"""Async engine, session factory and the request-scoped session dependency.

Why this file exists
--------------------
The engine owns a connection pool and must exist once per process; a session is
cheap, stateful and must exist once per unit of work. Getting that boundary
wrong is the most expensive mistake in a SQLAlchemy codebase — a shared session
leaks state between requests, and a session per query destroys the pool.

This module defines both lifetimes and exposes the only two ways to obtain a
session:

* :func:`get_session` — a FastAPI dependency giving one session per request.
* :func:`session_scope` — a context manager for code with no request: workers,
  CLI scripts, scheduled jobs.

Transaction policy
------------------
The **service** commits; repositories never do. Repositories add, delete and
flush. If a repository committed, a service could not make two writes atomic —
the first would already be durable when the second failed.

Pool sizing
-----------
The real ceiling is ``pool_size * workers * replicas`` against PostgreSQL's
``max_connections``. Exceeding it produces connection refusals under load, not
graceful slowness, and it is discovered during the traffic spike rather than
before it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _create_engine(url: str) -> AsyncEngine:
    """Build an async engine with the configured pool and timeouts.

    The server-side ``statement_timeout`` is the important part: without it a
    single pathological query holds its connection indefinitely, and enough of
    them exhaust the pool and take down every endpoint, not just the slow one.
    """
    return create_async_engine(
        url,
        echo=settings.database.echo,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
        pool_recycle=settings.database.pool_recycle,
        pool_pre_ping=settings.database.pool_pre_ping,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.database.statement_timeout_ms),
                "application_name": settings.app.name,
            }
        },
    )


#: Process-wide engine and connection pool. Disposed in the app lifespan.
engine: AsyncEngine = _create_engine(str(settings.database.url))

#: Optional read-replica engine. ``None`` until a replica is configured.
read_engine: AsyncEngine | None = (
    _create_engine(str(settings.database.read_replica_url))
    if settings.database.read_replica_url
    else None
)

#: Session factory.
#:
#: ``expire_on_commit=False`` matters specifically for async code: with the
#: default, touching any attribute after a commit triggers a lazy refresh, which
#: raises ``MissingGreenlet`` outside an await context. Keeping objects usable
#: after commit is what lets a router serialise what a service returned.
#:
#: ``autoflush=False`` makes writes explicit. Implicit flushes fire mid-query at
#: hard-to-predict moments and surface constraint violations far from the code
#: that caused them.
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
    injection is available. Commits on success and rolls back on failure —
    appropriate because here the caller *is* the unit of work.

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


async def check_database_health() -> bool:
    """Verify the database answers a trivial query.

    Used by the readiness probe and at startup. ``SELECT 1`` deliberately
    touches no table: a health check that queries application data reports
    "unhealthy" for a schema problem, and an orchestrator responds by killing
    replicas that were serving traffic perfectly well.

    Returns:
        ``True`` when the database responded.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        return False
    return True


async def dispose_engine() -> None:
    """Close every pooled connection. Called from the application lifespan."""
    await engine.dispose()
    if read_engine is not None:
        await read_engine.dispose()
