# Dependency rules

The most important document here. Every other convention is a consequence of
this one.

## The rule

**Dependencies point inward. Nothing in `core`, `common` or `infrastructure`
may import from `app.modules`.**

```text
        app/modules/          (business features)
              │  may import everything below
              ▼
    app/infrastructure/       (adapters to external systems)
              │  may import core, common
              ▼
        app/core/             (framework wiring)
              │  may import common
              ▼
       app/common/            (pure, feature-agnostic helpers)
```

An import pointing the other way is not a style violation — it is a design
error that has already happened somewhere else. If `core` needs something from
a feature, that something is not framework wiring.

## Why it matters

Without the rule, three things fail, in this order:

1. **Circular imports.** `core.security` imports the user model; the user model
   imports `core.security` for hashing. Python raises `ImportError` at start-up
   and someone "fixes" it with a function-local import, which hides the cycle
   rather than removing it.
2. **Untestable units.** A `common` helper that imports a feature drags the
   database, the settings and half the app into a test that should have needed
   none of them.
3. **The architecture stops holding.** Once one inward import exists, the next
   is easier to justify, and within a year every layer imports every other.

## Layer-by-layer

### `app/common/`

Imports: the standard library, third-party libraries, `app.core.config` and
`app.core.exceptions`.

Never imports: `app.modules`, `app.infrastructure`.

The test: **could this function be copied into an unrelated project
unchanged?** If not, it is not common.

### `app/core/`

Imports: `app.common`, third-party libraries.

Never imports: `app.modules`, and — for the bootstrap modules — nothing that
would run at import time. `config.py` is imported by almost everything, so an
expensive import there is paid for everywhere.

### `app/infrastructure/`

Imports: `app.core`, `app.common`, the external client libraries.

Never imports: `app.modules`. Infrastructure knows how to store *bytes*, not
what an invoice is.

Each subsystem exposes an interface (`Cache`, `StorageProvider`,
`EmailProvider`) and one or more implementations. Features depend on the
interface.

### `app/modules/<feature>/`

Imports: everything, plus other modules' **services** — never their
repositories or models.

## Within a module

```text
router.py  →  dependencies.py  →  service.py  →  repository.py  →  models.py
```

| Layer | May import | Must never |
| --- | --- | --- |
| `router.py` | schemas, dependencies | build SQL, contain business rules, import another module's service |
| `service.py` | repositories, infrastructure interfaces, events | import `fastapi`, raise `HTTPException`, build SQL |
| `repository.py` | models, `app.infrastructure.database` | apply business rules, call services, `commit()` |
| `models.py` | `app.infrastructure.database`, other models | contain behaviour beyond trivial properties |
| `schemas.py` | `app.common.types` | import models, touch the database |

### The two tests

**Could this service run from a CLI script with no HTTP server?** If not,
transport has leaked into business logic. A service importing `fastapi` fails
this immediately.

**Could this repository be read without knowing the business rules?** If it
contains an `if` that encodes policy — "only send if the account is active" —
that belongs in the service.

## Cross-module dependencies

Call the other module's **service**, never its repository or models directly.
A repository applies no business rules, so reaching past the service skips
validation, events and authorization silently.

When two modules need each other, you have three options, in order of
preference:

1. **Publish an event.** `billing` reacts to `subscription.cancelled` without
   `subscriptions` knowing `billing` exists. Correct whenever the caller does
   not need a result.
2. **Extract the shared logic** into a third module both depend on.
3. **Accept a one-way dependency** and document why.

Never resolve a cycle with a function-local import. It works, and it converts a
visible architectural problem into an invisible one.

## Enforcement

Ruff's `TID252` bans relative imports, so every import states its full path and
a violation is greppable:

```bash
# Should return nothing:
grep -rn "from app.modules" app/core app/common app/infrastructure
```

Worth adding to CI as an explicit check once the first modules exist — a rule
that is only enforced by review is enforced intermittently.
