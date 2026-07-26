"""Alembic migration environment.

Why this file exists
--------------------
Alembic needs two things to autogenerate a migration: a connection to the live
database and the ``MetaData`` describing the desired schema. This module
supplies both — and, critically, imports every model module so they are
registered on that metadata first.

**That import step is the one thing developers forget.** A model that is not
imported here is invisible to autogenerate, so ``alembic revision
--autogenerate`` produces an empty migration and the table is never created —
silently, with no error.

Configuration comes from :mod:`app.core.config`, not from ``alembic.ini``, so
migrations and the application can never disagree about which database they are
talking to.

Async note
----------
The engine uses asyncpg, so migrations run inside ``asyncio.run``. Alembic's
migration context itself is synchronous and executes via ``run_sync``.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.infrastructure.database.base import Base

# --- Model registration ----------------------------------------------------
# Import every module's `models` here so its tables register on Base.metadata.
# Autogenerate only sees what has been imported.
#
# TODO: import each app.modules.<feature>.models module as features are added.
#   from app.modules.billing import models as billing_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The application is the single source of truth for the connection URL.
config.set_main_option("sqlalchemy.url", str(settings.database.url))

#: Schema autogenerate diffs the database against.
target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    """Configure the migration context for a live connection.

    ``compare_type`` and ``compare_server_default`` are enabled so a changed
    column type or default is detected. They produce occasional false positives
    — always read a generated migration before applying it.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database.

    For environments where a DBA applies migrations by hand and the application
    has no DDL privileges.
    """
    context.configure(
        url=str(settings.database.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    """Run migrations on a synchronous connection facade."""
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect and apply migrations.

    ``NullPool`` because a migration run is short-lived: pooling would hold
    connections open after the work is done.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
