# Architecture

The project's constitution. When you are unsure where code belongs or how it
should be written, the answer is here — and if it is not, that is a gap worth
filling rather than a decision to make ad hoc.

Read [`dependency-rules.md`](dependency-rules.md) first. Everything else follows
from it.

| Document | Answers |
| --- | --- |
| [`project-structure.md`](project-structure.md) | Where does this code go? |
| [`dependency-rules.md`](dependency-rules.md) | What may import what? |
| [`request-lifecycle.md`](request-lifecycle.md) | What happens to a request, in order? |
| [`coding-standards.md`](coding-standards.md) | How is code written here? |
| [`api-guidelines.md`](api-guidelines.md) | What does the API look like on the wire? |
| [`database-conventions.md`](database-conventions.md) | How are models, queries and migrations written? |
| [`error-handling.md`](error-handling.md) | How do failures travel and surface? |
| [`configuration.md`](configuration.md) | Where do settings and secrets come from? |
| [`security.md`](security.md) | How are tokens, passwords and keys handled? |
| [`observability.md`](observability.md) | How is the system understood in production? |
| [`testing-strategy.md`](testing-strategy.md) | What is tested, and at which layer? |
| [`adr/`](adr/) | Why was a decision made, and what was rejected? |

## The four layers

| Layer | Directory | Owns | May import |
| --- | --- | --- | --- |
| **Core** | `app/core/` | Framework wiring: settings, logging, lifespan, middleware, exception handlers, security primitives | `common` |
| **Infrastructure** | `app/infrastructure/` | Adapters to external systems: PostgreSQL, Redis, storage, email, queue | `core`, `common` |
| **Modules** | `app/modules/` | Business features | everything |
| **Common** | `app/common/` | Reusable, feature-agnostic helpers | `core` (config only) |

Plus two small packages that are neither: `app/events/` (domain events) and
`app/system/` (operational endpoints — health, JWKS).

**Dependencies point inward.** Nothing in `core`, `common` or `infrastructure`
may import from `modules`. If you need to, the thing you are reaching for is not
infrastructure — it is business logic in the wrong layer.

## Request flow

```text
HTTP Request
  └─ Middleware          request ID, CORS, access log, rate limit
     └─ Router           app/modules/<feature>/router.py     paths, status codes
        └─ Dependency    dependencies.py                     builds the service
           └─ Service    service.py       business rules, transactions
              └─ Repository  repository.py   SQL, and nothing else
                 └─ Infrastructure  session, cache, storage
                    └─ PostgreSQL
```

Each arrow crosses exactly one layer. A router that touches a repository, or a
service that builds a `select()`, has collapsed a boundary — and the cost
appears later as logic that cannot be tested without HTTP, or a query that
cannot be optimised without reading five files.

## Why this shape

**Business logic must not depend on infrastructure implementations.** Services
depend on `Cache`, `EmailProvider`, `StorageProvider` — protocols — not on
`redis.asyncio.Redis`, `aiosmtplib` or `boto3`. Three consequences:

1. Services are testable without a network, a broker or credentials.
2. Swapping S3 for R2, or SMTP for an API, touches one file.
3. Business rules stay readable, because they are not interleaved with
   connection handling and retries.

**Feature-first beats layer-first at scale.** Grouping by technical role
(`models/`, `services/`, `routers/`) is fine at twenty files. At four hundred, a
single change touches five distant directories, unrelated features collide in
the same files, and nothing in the tree says what the product does.

## Decisions already made

Recorded in [`adr/`](adr/), and expensive to revisit:

| Decision | Choice |
| --- | --- |
| API style | snake_case, enveloped responses, URL versioning |
| Primary keys | UUIDv7, generated in Python |
| Multi-tenancy | Row-level `tenant_id`, enforced in the repository base |
| Token signing | Ed25519 (EdDSA) with `kid`-based rotation and JWKS |
| Password hashing | Argon2id via pwdlib, with transparent rehashing |
| Transaction boundary | The service commits; repositories never do |

## Still open

Each needs an answer before the feature that depends on it, and each is
expensive to change afterwards:

- **Queue backend** — arq, Dramatiq, Celery or Redis Streams.
- **Authorization model** — RBAC, ABAC or scoped permissions.
- **Soft deletion** — global policy or per-feature.
- **Search** — PostgreSQL full-text first, or a dedicated engine.
