"""Integration tests for :mod:`app.infrastructure`.

Mirrors the source layout: ``database/``, ``redis/``, ``storage/``, ``email/``,
``queue/``.

The migration test is the important one here: apply every migration to an empty
database and assert the resulting schema matches ``Base.metadata``. That is what
catches a model added without a migration.
"""
