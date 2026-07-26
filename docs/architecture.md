# Architecture

## The four layers

| Layer | Directory | Owns | May import |
|---|---|---|---|
| **Core** | `app/core/` | Framework wiring: settings, logging, lifespan, middleware, exception handlers, security primitives | `common` |
| **Infrastructure** | `app/infrastructure/` | Adapters to external systems: PostgreSQL, Redis, storage, email, queue | `core`, `common` |
| **Modules** | `app/modules/` | Business features | everything |
| **Common** | `app/common/` | Reusable, feature-agnostic helpers | `core` (config only) |

**Dependencies point inward.** Nothing in `core`, `common` or `infrastructure`
may import from `modules`. If you find yourself needing to, the thing you are
reaching for is not infrastructure — it is business logic that ended up in the
wrong layer.

## Request flow

```
HTTP Request
  └─ Router            app/modules/<feature>/router.py     paths, status codes
       └─ Dependency   app/modules/<feature>/dependencies.py  builds the service
            └─ Service     service.py       business rules, transactions
                 └─ Repository  repository.py   SQL, and nothing else
                      └─ Infrastructure  session, cache, storage
                           └─ PostgreSQL
```

Each arrow crosses exactly one layer. A router that touches a repository, or a
service that builds a `select()`, has collapsed a boundary — and the cost shows
up later as logic that cannot be tested without HTTP, or a query that cannot be
optimised without reading five files.

## Why this shape

**Business logic must not depend on infrastructure implementations.** A service
depends on `Cache`, `EmailProvider`, `StorageProvider` — protocols. Not on
`redis.asyncio.Redis`, `aiosmtplib` or `boto3`. Three consequences follow:

1. Services are testable without a network, a broker or credentials.
2. Swapping S3 for R2, or SMTP for a transactional API, touches one file.
3. The business rules stay readable, because they are not interleaved with
   connection handling and retry logic.

**Feature-first beats layer-first at scale.** Grouping by technical role
(`models/`, `services/`, `routers/`) is fine at twenty files. At four hundred, a
single change touches five distant directories, unrelated features collide in
the same files, and nothing in the tree says what the product does.

## Transaction boundaries

The **service** commits. Repositories `add`, `delete` and `flush`, never
`commit`.

This is not a style preference. If a repository commits, a service cannot make
two writes atomic — the first is already durable when the second fails. Placing
the boundary at the service is what lets one business operation be one
transaction.

The session is request-scoped: one per request, provided by the `get_session`
dependency, rolled back if the request raises.

## Error handling

Services raise `AppError` subclasses (see
[`app/core/exceptions.py`](../app/core/exceptions.py)). They never raise
`HTTPException` — a service must be callable from a worker or a CLI, and it
cannot be if it speaks HTTP.

Global handlers translate those errors into one response envelope:

```json
{"error": {"code": "thing_not_found", "message": "Thing not found."}}
```

`code` values are a public contract. Adding one is safe; changing one breaks
clients.

## Configuration

One `Settings` object, built once at import in
[`app/core/config.py`](../app/core/config.py), validated at startup. Nested
models per subsystem, populated with a `__` delimiter:

```
APP__ENVIRONMENT=production
DATABASE__URL=postgresql+asyncpg://…
JWT__ACCESS_TOKEN_EXPIRE_MINUTES=15
```

Nothing else reads `os.environ`. A missing required value is a startup crash
with a precise error, not a `None` discovered in production.

## Events

Cross-feature reactions go through the event bus
([`app/events/`](../app/events/)) rather than direct service-to-service calls,
so a new reaction is a new subscriber instead of an edit to the publishing
feature.

Use an event when the publisher does not need the outcome. Use a direct call
when it does, or when the work must be in the same transaction.

## Deferred work

Anything slow, retryable or third-party is enqueued
([`app/infrastructure/queue/`](../app/infrastructure/queue/)), never awaited
inline. A request handler awaiting a five-second third-party call holds a
worker, a database connection and the client's socket for five seconds — and
fails the request when that third party has a bad minute.

## Open decisions

These need answers before the first feature ships, because each is expensive to
change afterwards:

- **Multi-tenancy model** — row-level `tenant_id`, schema per tenant, or
  database per tenant. Affects every model and every query.
- **Primary keys** — UUIDv4, UUIDv7 (time-ordered, better index locality), or
  bigint identity.
- **Queue backend** — arq, Dramatiq, Celery or Redis Streams.
- **API versioning** — URL prefix vs header negotiation.
- **Soft deletion** — whether it applies globally or per feature.
