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

# --- Model registration ----------------------------------------------------
# Every feature's `models` module must be imported before autogenerate runs, or
# its tables are absent from Base.metadata and the generated migration is
# silently empty. Discovery does this, so there is no import list to forget.
# Infrastructure models are not features, so they are imported by name.
import app.infrastructure.outbox.models  # noqa: F401
from app.core.config import settings
from app.core.discovery import import_side_effect_modules
from app.infrastructure.database.base import Base

# Feature models come from discovery; see app/core/discovery.py.
import_side_effect_modules()

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is essential. The default is True, which
    # silently disables every logger configured before this point — so running a
    # migration in-process (a test suite, a startup hook) leaves the application
    # unable to log anything afterwards, with no error to explain it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The application settings are the source of truth for the connection URL, so
# migrations and the app can never disagree about which database they mean.
#
# But a caller that has *explicitly* set a URL — the test suite pointing at
# `genesis_test`, or tooling targeting a replica — must win. Overriding
# unconditionally is worse than it looks: it silently migrates the development
# database whenever the suite runs, and the failure surfaces much later as a
# table that exists in the wrong place.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", str(settings.database.url))

#: Schema autogenerate diffs the database against.
target_metadata = Base.metadata


#: Tables with this prefix are declared by the test suite on ``Base.metadata``
#: and created inside a transaction that is rolled back. They use the real
#: declarative base deliberately, so they inherit the real naming convention and
#: type mapping — which also means autogenerate would otherwise try to create
#: them.
TEST_TABLE_PREFIX = "_test_"


def _include_object(_obj, name, type_, reflected, compare_to) -> bool:
    """Decide whether autogenerate should consider a schema object.

    Two distinct jobs:

    * Skip the throwaway tables the test suite puts on ``Base.metadata``.
    * Skip *reflected* tables the application does not own. Without this,
      autogenerate treats anything present in the database but absent from the
      metadata as something to remove, and writes ``op.drop_table()`` for a
      table belonging to another service, an extension, or a colleague's work in
      progress. Generated migrations are reviewed by a human, but that is a thin
      defence against a diff that otherwise looks entirely routine.
    """
    if type_ == "table" and name is not None and name.startswith(TEST_TABLE_PREFIX):
        return False
    return not (type_ == "table" and reflected and compare_to is None)


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
        include_object=_include_object,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database.

    For environments where a DBA applies migrations by hand and the application
    has no DDL privileges.
    """
    # Reads the *resolved* option rather than `settings.database.url`. The block
    # above already reconciled "a caller supplied a URL" with "fall back to
    # settings"; going back to settings here ignores an explicitly targeted
    # database and emits DDL for whatever the environment happens to point at.
    # That is the same bug the online path had — in the one mode whose output is
    # handed to a DBA to run against production.
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
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
