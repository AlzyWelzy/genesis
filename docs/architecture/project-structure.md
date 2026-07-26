# Project structure

## Where things go

```text
app/
├── main.py                  composition root — builds the app, nothing else
├── api.py                   root router; every feature router is included here
│
├── core/                    framework wiring
│   ├── settings.py          typed configuration surface
│   ├── config.py            the one Settings instance
│   ├── context.py           request-scoped ContextVars
│   ├── security/            passwords, tokens, keys, JWKS
│   ├── logging.py           root handler, JSON format, redaction
│   ├── lifespan.py          startup and shutdown
│   ├── middleware.py        the ordered middleware stack
│   ├── exceptions.py        error hierarchy and global handlers
│   └── openapi.py           schema customisation
│
├── infrastructure/          adapters to external systems
│   ├── database/            engine, session, Base, mixins, repository bases
│   ├── redis/               client, cache, pub/sub, rate limiting
│   ├── storage/             object storage behind a provider protocol
│   ├── email/               transport behind a provider protocol
│   ├── queue/               background jobs
│   └── observability/       metrics and tracing seams
│
├── common/                  reusable, feature-agnostic
│   ├── constants.py         limits, headers, formats
│   ├── enums.py             shared enumerations
│   ├── types.py             type aliases, BaseSchema
│   ├── responses.py         response envelopes
│   ├── pagination.py        offset and cursor pagination
│   ├── sorting.py           allow-listed sort resolution
│   ├── filtering.py         typed filter schemas
│   ├── dependencies.py      shared DI + the per-module pattern
│   └── utils/               pure helpers
│
├── events/                  domain events and the in-process bus
├── system/                  health probes, JWKS — operational, not business
└── modules/                 business features, one package each

migrations/                  Alembic
scripts/                     operational entry points
tests/                       mirrors app/
docs/architecture/           this constitution
```

## Feature-first

A feature lives in exactly one directory:

```text
app/modules/billing/
├── router.py        HTTP layer
├── service.py       business logic
├── repository.py    data access
├── models.py        SQLAlchemy models
├── schemas.py       Pydantic request/response
├── dependencies.py  DI wiring
├── exceptions.py    feature errors
└── constants.py     feature constants
```

Grouping by technical role (`models/`, `services/`, `routers/`) is fine at
twenty files. At four hundred: a single change touches five distant
directories, unrelated features collide in the same files, and nothing in the
tree says what the product does.

Feature-first means a change is a diff in one directory, the tree reads as a
list of capabilities, and extracting a feature into its own service is moving a
folder.

Full per-file responsibilities: [`app/modules/README.md`](../../app/modules/README.md).

## Decision table

| You are adding | It goes in |
| --- | --- |
| An endpoint | `app/modules/<feature>/router.py` |
| A business rule | `app/modules/<feature>/service.py` |
| A query | `app/modules/<feature>/repository.py` |
| A table | `app/modules/<feature>/models.py` + a migration |
| A setting | `app/core/settings.py` |
| A constant used by one feature | that module's `constants.py` |
| A constant used by three features | `app/common/constants.py` |
| A pure helper | `app/common/utils/` |
| A new external system | `app/infrastructure/<system>/` |
| A health or ops endpoint | `app/system/router.py` |
| A cross-feature reaction | an event subscriber |

## Naming

| Kind | Convention | Example |
| --- | --- | --- |
| Module directory | singular | `app/modules/billing/` |
| Table | plural snake_case | `invoice_line_items` |
| Route path | plural | `/api/v1/invoices` |
| Router tag | matches the module | `billing` |
| Schema | `<Resource><Direction>` | `InvoiceCreate`, `InvoiceRead` |
| Service | `<Resource>Service` | `InvoiceService` |
| Repository | `<Resource>Repository` | `InvoiceRepository` |
| Test file | mirrors the source | `tests/modules/billing/test_service.py` |

## When to split a file

At roughly 400 lines, and not before. A `service.py` becomes a `service/`
package keeping the same public import path:

```text
service/
├── __init__.py      re-exports InvoiceService
├── creation.py
└── reconciliation.py
```

A 300-line file that reads top to bottom beats four files that must be read
together. Do not create a file before it has content — a module with three
endpoints and no custom errors does not need an empty `exceptions.py`.
