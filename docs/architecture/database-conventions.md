# Database conventions

## Primary keys

**UUIDv7, generated in Python** via `UUIDPrimaryKeyMixin`.

UUID over auto-increment: IDs can be minted client-side, they do not leak row
counts or growth rate, `/users/1` cannot be walked to `/users/2`, and rows
survive being merged across environments.

**v7 over v4**: a v7 UUID embeds a millisecond timestamp in its high bits, so
new keys sort together and inserts land at the right edge of the B-tree. Random
v4 keys scatter writes across the whole index — constant page splits and a
collapsing cache hit rate on large tables. v7 keeps v4's opacity and recovers
most of a sequential key's write locality.

Generated in Python rather than by the database so the application knows the ID
before the `INSERT`, which lets it build related rows and publish events in one
pass without a round trip.

## Naming

Set by `NAMING_CONVENTION` in
[`naming.py`](../../app/infrastructure/database/naming.py) and applied to the
shared `MetaData`. Without it PostgreSQL invents names that Alembic cannot
predict, producing migrations that apply on a fresh database and fail on
production.

| Object | Pattern | Example |
| --- | --- | --- |
| Table | plural snake_case | `invoice_line_items` |
| Column | singular snake_case | `due_date` |
| Foreign key column | `<singular>_id` | `invoice_id` |
| Boolean | `is_` / `has_` prefix | `is_active` |
| Timestamp | `_at` suffix | `created_at`, `deleted_at` |
| Index | `ix_<table>_<columns>` | `ix_invoices_tenant_id` |
| Unique | `uq_<table>_<columns>` | `uq_users_email` |
| Constraint | `ck_<table>_<name>` | `ck_invoices_positive_total` |

## Multi-tenancy

**Row-level `tenant_id`, enforced in the repository base.**

Every tenant-scoped model carries `TenantMixin`. Every query goes through
`TenantRepository.select()`, which applies the tenant predicate before the
caller sees the statement.

This is structural rather than conventional for one reason: a single missed
`WHERE tenant_id = ?` returns another customer's data with a 200 status and
nothing in the response, the logs or the tests to indicate it. It is the
highest-severity bug this architecture can produce and the easiest to write.

The tenant comes from `app.core.context`, never from a method argument or a
request body. A parameter can be passed wrongly or passed from user input; the
context is populated once, at the edge, by an authenticated dependency.

```python
class InvoiceRepository(TenantRepository[Invoice]):
    model = Invoice

    async def list_overdue(self, params: PaginationParams) -> Sequence[Invoice]:
        """Overdue invoices for the current tenant."""
        stmt = self.select().where(Invoice.due_date < utc_now())  # scoped already
        ...
```

Writing `select(Invoice)` by hand bypasses scoping. That is intentional — it is
sometimes needed, and it must be visible in review and greppable.

### Indexing

Effectively every query starts with `WHERE tenant_id = ?`, so:

- `tenant_id` is always indexed.
- Composite indexes lead with `tenant_id`: `(tenant_id, status)` serves both
  "this tenant" and "this tenant's active rows"; `(status, tenant_id)` serves
  neither well, because PostgreSQL can only use a leading prefix.
- A tenant-only index is not enough on a large table — it still leaves the
  database sifting through one tenant's entire history.

## Timestamps

Timezone-aware, always, set by the **database** via `server_default` and
`onupdate` so rows written by a migration or by hand are also correct.

`TimestampMixin` provides `created_at` / `updated_at`. In Python, the only
sanctioned clock read is `app.common.utils.datetime.utc_now()` — one function
to freeze in a test, and no naive datetimes anywhere.

## Soft deletion

`SoftDeleteMixin` adds `deleted_at`. Apply it deliberately, not by default.

It is not free: unique constraints must become **partial**
(`WHERE deleted_at IS NULL`), or a user cannot re-register an email address they
previously deleted. Every read path must filter it, which is why the filter
belongs in the repository rather than at each call site.

## Queries

Repositories own all SQL. They:

- build statements and execute them;
- apply eager loading, pagination, sorting and scoping;
- `flush()` when the caller needs a generated ID.

They never apply business rules, call services, publish events or `commit()`.

**Eager-load relationships used in a response.** Lazy loading in an async
session raises `MissingGreenlet` at best, and issues N+1 queries at worst:

```python
stmt = self.select().options(selectinload(Invoice.line_items))
```

`selectinload` for collections (one extra query), `joinedload` for many-to-one
(one join). A `joinedload` on a collection multiplies rows by the collection
size.

**Never interpolate client input into SQL.** Sortable and filterable columns
come from explicit allow-lists — see
[`app/common/sorting.py`](../../app/common/sorting.py).

**Enforce invariants in the database too.** An application-level uniqueness
check loses to concurrency: two simultaneous requests both see "no existing
row" and both insert. A unique index does not lose. Catch the resulting
`IntegrityError` and translate it into a `ConflictError`.

## Transactions

The service commits; repositories never do. See
[`request-lifecycle.md`](request-lifecycle.md).

`autoflush=False` is set deliberately — implicit flushes fire mid-query at
hard-to-predict moments and surface constraint violations far from the code
that caused them. Flush explicitly when you need an ID.

`expire_on_commit=False` is required for async: with the default, touching any
attribute after a commit triggers a lazy refresh and raises `MissingGreenlet`.

## Migrations

Full rules in [`migrations/README.md`](../../migrations/README.md). The three
that cause outages:

1. **`env.py` must import every model module.** Autogenerate diffs against
   `Base.metadata`; a model never imported is not on it, so Alembic generates
   an empty migration and the table is silently never created.
2. **`ALTER TABLE ... ADD COLUMN NOT NULL` without a default rewrites the whole
   table** and blocks writes for its duration. Add nullable, backfill in
   batches, then add the constraint.
3. **`CREATE INDEX` blocks writes.** Use `CONCURRENTLY` on any table with
   traffic — and note it cannot run inside a transaction, so the migration needs
   `op.get_context().autocommit_block()`.

Deploy destructive changes across two releases: during a rolling deploy the old
code is still running, so first ship code that no longer uses the column, then
ship the migration that drops it.
