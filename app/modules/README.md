# Modules — feature-first organisation

Every business feature lives in exactly one package here. **This directory is
intentionally empty**: no module exists yet, and none should be created until
there is a real feature to build.

## Why feature-first

The alternative — grouping by technical role (`models/`, `services/`,
`routers/`) — looks tidy at twenty files and fails at four hundred. Changing one
feature means touching five distant directories, two developers on unrelated
features constantly collide, and nothing in the tree tells you what the product
actually does.

Grouping by feature means a change is a diff in one directory, the tree reads as
a list of capabilities, and a feature can be extracted into its own service by
moving one folder.

## Structure of a module

```
app/modules/<feature>/
├── __init__.py
├── router.py        # HTTP layer
├── service.py       # business logic
├── repository.py    # data access
├── models.py        # SQLAlchemy ORM models
├── schemas.py       # Pydantic request/response schemas
├── dependencies.py  # FastAPI dependency wiring
├── exceptions.py    # feature-specific errors
└── constants.py     # feature-specific constants
```

Do not create a file before it has content. A module with three endpoints and no
custom errors does not need an empty `exceptions.py`.

Split a file into a package (`service/` with submodules) when it passes roughly
400 lines — never earlier, and keep the same public import path.

---

## File responsibilities

### `router.py` — the HTTP layer

Owns URLs, status codes, and the request/response contract. Declares one
`APIRouter` with its own `prefix` and `tags`, included by
[`app/api.py`](../api.py).

An endpoint should read as four lines: resolve dependencies, call one service
method, return the result.

- **Does:** declare paths, methods, status codes, `response_model`, and
  auth/permission dependencies.
- **Does not:** query the database, contain `if` branches implementing business
  rules, or import another module's service.
- **Rule:** no `await session.execute(...)` in a router, ever.
- **Rule:** never return an ORM model directly. Convert through a response
  schema, or you will eventually leak a password hash by adding a column.

### `service.py` — business logic

The heart of the module and the only layer that encodes business rules,
orchestration and transaction boundaries.

- **Does:** validate business invariants, coordinate repositories, call
  infrastructure through its abstractions, publish domain events, own
  `commit()`.
- **Does not:** import `fastapi`, touch `Request`/`Response`, build SQL, or
  raise `HTTPException`.
- **Rule:** raise `AppError` subclasses from
  [`app/core/exceptions.py`](../core/exceptions.py). A service must be callable
  from a worker or a CLI, and it cannot be if it speaks HTTP.
- **Rule:** the service commits, repositories do not. Only the service knows
  whether three writes are one atomic operation.
- **Cross-module calls:** call the other module's *service*, never its
  repository. If that creates a cycle, the shared logic belongs in a third
  module or an event.

### `repository.py` — data access

The only place SQL is written. Translates between the database and domain
objects.

- **Does:** build `select()`/`insert()`/`update()` statements, apply eager
  loading, apply pagination and sorting, `flush()` when an ID is needed.
- **Does not:** apply business rules, call other services, send email, or
  `commit()`.
- **Rule:** never interpolate a client-supplied string into SQL. Sortable and
  filterable columns come from an explicit allow-list.
- **Rule:** an endpoint that returns a list must eager-load its relationships.
  Lazy loading in an async session raises `MissingGreenlet` at best and issues
  N+1 queries at worst.
- **Rule:** every list method takes `PaginationParams`. There is no unbounded
  `list_all()`.

### `models.py` — persistence

SQLAlchemy ORM models: tables, columns, relationships, indexes and constraints.

- **Does:** inherit `Base` from
  [`app/infrastructure/database/base.py`](../infrastructure/database/base.py)
  and compose the mixins from
  [`mixins.py`](../infrastructure/database/mixins.py).
- **Does not:** contain business methods, validation, or serialisation logic.
  A model is a row, not a domain service.
- **Rule:** every model must be imported by `migrations/env.py`, or Alembic will
  not see it and its table will never be created.
- **Rule:** enforce invariants in the database too. Application-level uniqueness
  checks lose to concurrency; a unique index does not.

### `schemas.py` — API contract

Pydantic models defining what the API accepts and returns. Inherit from
`BaseSchema` / `ORMSchema` in [`app/common/types.py`](../common/types.py).

Name by direction, and keep them separate — a shared schema forces every
optional-on-update field to be optional on create too:

```
ThingCreate    # request body for POST
ThingUpdate    # request body for PATCH — all fields optional
ThingRead      # response body
ThingFilter    # query parameters
```

- **Does:** declare fields, constraints, examples and field-level validators.
- **Does not:** query the database or import ORM models.
- **Rule:** a `Read` schema is an allow-list. Never build one with
  `model_config = ConfigDict(extra="allow")` over an ORM object.

### `dependencies.py` — injection wiring

Assembles the objects a router needs: session → repository → service, plus
resource-loading and permission dependencies.

```python
async def get_thing_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ThingService:
    return ThingService(ThingRepository(session))
```

- **Does:** construct services, resolve path parameters into entities, enforce
  authorisation.
- **Does not:** contain business logic. A dependency that decides *what to
  charge* is a service in disguise.
- **Rule:** this is where a router gets a service. A router must never
  instantiate `ThingService(...)` itself.

### `exceptions.py` — feature errors

Domain errors specific to the feature, subclassing the base types in
[`app/core/exceptions.py`](../core/exceptions.py).

```python
class ThingNotFoundError(NotFoundError):
    code = "thing_not_found"
    message = "Thing not found."
```

- **Rule:** subclass an existing base so the global handler already knows the
  status code. Never register a new exception handler per module.
- **Rule:** `code` values are a public API contract. Adding one is safe;
  changing one is a breaking change.

### `constants.py` — feature constants

Values used by this feature only: limits, cache-key prefixes, event names,
default values.

- **Rule:** anything that varies by environment is configuration and belongs in
  [`app/core/settings.py`](../core/settings.py) instead.
- **Rule:** anything genuinely used by three or more modules moves to
  [`app/common/constants.py`](../common/constants.py) — but move it only when
  the third use arrives, not in anticipation.

---

## The dependency rule

```
router → dependencies → service → repository → infrastructure → PostgreSQL
```

Dependencies point one way. A repository never calls a service; a service never
imports `fastapi`; nothing in `app/core` or `app/common` ever imports from
`app/modules`.

The practical test: **could this service run from a CLI script with no HTTP
server?** If not, transport has leaked into business logic.

## Checklist for a new module

1. Create `app/modules/<feature>/` with `__init__.py`.
2. Define `models.py`, register it in `migrations/env.py`, generate a migration.
3. Define `schemas.py` — request and response shapes.
4. Implement `repository.py` — queries only.
5. Implement `service.py` — rules, orchestration, transactions.
6. Wire `dependencies.py`.
7. Write `router.py` and include it in [`app/api.py`](../api.py).
8. Add tests under `tests/modules/<feature>/` — see
   [`tests/README.md`](../../tests/README.md).
