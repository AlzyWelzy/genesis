"""Declarative base and shared model metadata.

Why this file exists
--------------------
Every ORM model must attach to the *same* ``MetaData`` object. That object is
what Alembic diffs against the live database, so a model registered on a second
metadata is invisible to autogenerate and its table silently never gets created.

Centralising the base also gives one place to install cross-cutting model
policy: the constraint naming convention, the Python-to-SQL type map, and the
default table-name derivation. Individual models then declare only what is
actually specific to them.

Import discipline
-----------------
Models import ``Base`` from here. This module imports nothing from
:mod:`app.modules` — the dependency points inward only. Alembic's ``env.py``
must import every model module so they register on this metadata before
autogenerate runs; see ``migrations/env.py``.
"""

from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from app.infrastructure.database.naming import DEFAULT_SCHEMA, NAMING_CONVENTION
from app.infrastructure.database.types import TYPE_ANNOTATION_MAP


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the application.

    Subclasses inherit the naming convention and the type annotation map, so a
    model can be declared with plain annotations::

        class Example(Base):
            __tablename__ = "examples"
            id: Mapped[UUID] = mapped_column(primary_key=True)
            created_at: Mapped[datetime]

    and still get ``TIMESTAMP WITH TIME ZONE`` and deterministic constraint
    names without repeating configuration.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION, schema=DEFAULT_SCHEMA)

    #: Maps Python annotations to concrete PostgreSQL column types.
    type_annotation_map: dict[Any, Any] = TYPE_ANNOTATION_MAP

    def __repr__(self) -> str:
        """Readable representation showing only the primary key.

        Deliberately excludes column values: a model may hold password hashes
        or tokens, and reprs end up in logs and tracebacks.
        """
        pk = self.__mapper__.primary_key_from_instance(self)
        return f"<{type(self).__name__} {pk}>"


# Decisions already encoded elsewhere, recorded here so a reader of this file
# does not have to go looking:
#
# * Primary keys are UUIDv7, supplied by `UUIDPrimaryKeyMixin` in mixins.py.
#   Time-ordered, so inserts keep index locality, without a sequential key's
#   enumerability.
# * Tenancy is row-level `tenant_id`, supplied by `TenantMixin` and *enforced*
#   by `TenantRepository`, which scopes every query it builds.
# * Soft deletion is opt-in per model via `SoftDeleteMixin`, and the
#   `deleted_at IS NULL` filter is applied by `SoftDeleteRepositoryMixin` —
#   never by individual call sites, which is where it would be forgotten.
#
# See docs/architecture/database-conventions.md.

__all__ = ["Base"]
