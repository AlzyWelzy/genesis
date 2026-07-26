# Migrations

Alembic manages the PostgreSQL schema. Configuration lives in
[`alembic.ini`](../alembic.ini); the connection URL comes from the application
settings via [`env.py`](env.py), never from the ini file.

## The rule that catches everyone

`env.py` must **import every model module**. Autogenerate diffs the database
against `Base.metadata`, and a model that was never imported is not on that
metadata — so Alembic generates an empty migration and the table is silently
never created. Add the import in the same commit as the model.

## Workflow

```bash
uv run alembic revision --autogenerate -m "add billing accounts"
uv run alembic upgrade head
```

**Always read the generated migration before applying it.** Autogenerate is a
starting point, not an oracle. It routinely misses table and column renames
(emitting a destructive drop-and-add instead), server default changes, and
CHECK constraints — and it happily generates an index creation that will lock a
production table.

## Other commands

```bash
uv run alembic current              # applied revision
uv run alembic history --verbose    # full history
uv run alembic downgrade -1         # roll back one revision
uv run alembic upgrade head --sql   # emit SQL without applying
```

## Rules

- **One logical change per migration.** Reviewable, and revertible on its own.
- **`downgrade()` must actually work.** Write it, and test it locally by
  applying and reverting.
- **Never edit an applied migration.** Once it is on any shared database its
  hash is recorded; changing it desynchronises every environment. Write a new
  one.
- **Separate schema and data migrations.** Mixing them means a failed data step
  rolls back the schema step too, and data changes need batching that schema
  changes do not.
- **Deploy destructive changes in two releases.** During a rolling deploy the
  old code is still running: first ship code that no longer uses the column,
  then ship the migration dropping it.
- **`CREATE INDEX CONCURRENTLY`** for any table with real traffic — and note it
  cannot run inside a transaction, so the migration needs
  `op.get_context().autocommit_block()`.
- **Adding a `NOT NULL` column with no default rewrites the table.** Add it
  nullable, backfill in batches, then add the constraint.

## Bootstrap

No migrations exist yet. The first one should be generated once the first
feature module defines its models — and it must be reviewed especially
carefully, since it establishes the constraint naming convention for everything
that follows.
