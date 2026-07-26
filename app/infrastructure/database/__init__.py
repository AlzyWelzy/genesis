"""PostgreSQL access layer.

Exposes the declarative :class:`~app.infrastructure.database.base.Base`, the
async engine and session factory, reusable model mixins, the shared column
types and the Alembic naming convention.

Only repositories should import from this package. A service that imports a
session factory has reached past its layer.
"""
