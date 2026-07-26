# AGENTS.md

Operating instructions for AI agents working in this repository. Read this
before changing anything.

Human contributors should read it too — everything here is a real project rule,
not agent-specific etiquette.

---

## 1. What this project is

A production-grade FastAPI backend intended to grow to hundreds of endpoints.
It is currently **foundation and platform scaffolding**: every layer below the
business logic is built, tested and working. There are **no feature modules
yet**, and that is deliberate.

**Stage status**

| Stage | Contents | Status |
|---|---|---|
| 1 — Foundation | structure, config, logging, errors, security, database, migrations, lifespan, middleware, API conventions, DI | **complete** |
| 1b — Platform infrastructure | cache, queue, outbox, storage, email, rate limiting, events, observability | **complete** |
| 2 — Platform features | auth, users, organizations, audit, notifications, feature flags | **not started — do not begin without an explicit instruction** |
| 3 — Product | actual business modules | not started |

**Do not create anything under `app/modules/` unless explicitly asked.** The
directory is empty on purpose. Adding a `users` module "to make things concrete"
is the single most likely way to get this wrong.

---

## 2. Non-negotiable rules

These are enforced mechanically. Violating one fails CI.

### 2.1 The dependency rule

```
app/core  ─┐
app/common ├─→  never import from  app/modules
app/infrastructure ─┘
```

Dependencies point inward. `core`, `common` and `infrastructure` know nothing
about features. Checked by `scripts/check_dependency_rule.sh` in pre-commit and
CI.

If you need a feature's type in core, you need a **seam**, not an import. See
`app/core/principal.py` for the worked example: core declares a `Protocol`, the
feature registers an implementation at startup, and structural typing means
neither imports the other.

### 2.2 Layer responsibilities

```
Router → Dependency → Service → Repository → Infrastructure → PostgreSQL
```

| Layer | Does | Never does |
|---|---|---|
| `router.py` | paths, status codes, response models, auth dependencies | SQL, business rules, returning ORM objects |
| `dependencies.py` | builds services, resolves identity, authorises | business logic |
| `service.py` | business rules, orchestration, **commits** | imports `fastapi`, raises `HTTPException`, writes SQL |
| `repository.py` | builds and runs queries, eager loading, scoping | business rules, `commit()`, calling services |
| `models.py` | tables, columns, relationships, constraints | business methods, serialisation |

The practical test: **could this service run from a CLI script with no HTTP
server?** If not, transport has leaked into business logic.

### 2.3 The service commits; repositories never do

A repository that commits makes it impossible for a service to treat three
writes as one atomic operation — the first is already durable when the second
fails.

### 2.4 Tenant scoping is structural, not remembered

Every tenant-scoped model uses `TenantMixin`, and every repository for such a
model extends `TenantRepository`, whose `select()` applies the tenant predicate.
Writing `select(Model)` by hand in a repository bypasses this and is a
cross-tenant data leak — the highest-severity bug this architecture can produce,
and one that returns HTTP 200 with no error anywhere.

### 2.5 Errors

Services raise `AppError` subclasses from `app/core/exceptions.py`. Never
`HTTPException`. The global handlers translate them into one envelope:

```json
{"error": {"code": "not_found", "message": "...", "request_id": "..."}}
```

`code` values are a public API contract. Adding one is safe; changing one is a
breaking change.

### 2.6 Configuration

Read once, in `app/core/config.py`. Nothing else touches `os.environ`. A new
setting goes in the appropriate nested model in `app/core/settings.py`, with a
docstring saying what it does and what happens when it is wrong.

---

## 3. Before you write code

1. **Read the module docstring** of any file you are changing. Every file
   explains *why it exists*; the reasoning usually constrains the change.
2. **Check `docs/architecture/`** for the relevant guide.
3. **Search for an existing helper.** Utilities live in `app/common/utils/`;
   infrastructure abstractions in `app/infrastructure/`. Reimplementing one
   locally is a review rejection.

---

## 4. Commands

```bash
uv sync                          # install
uv run python main.py            # dev server
uv run pytest                    # all tests
uv run pytest -m "not integration"   # no services required
uv run ruff check --fix . && uv run ruff format .
uv run ty check
./scripts/check_dependency_rule.sh
uv run alembic revision --autogenerate -m "..."
uv run alembic upgrade head
uv run python scripts/worker.py  # background jobs
```

Integration tests need PostgreSQL and Redis. They **skip** rather than fail when
those are absent, so a green run with everything skipped is not a green run —
check the summary line.

---

## 5. Adding a feature module (Stage 2+)

Only when asked. The structure is fixed:

```
app/modules/<feature>/
├── __init__.py
├── router.py        # must export `router: APIRouter` with its own prefix+tags
├── service.py
├── repository.py
├── models.py        # must be imported → discovery handles this
├── schemas.py
├── dependencies.py
├── handlers.py      # event subscriptions (optional)
├── tasks.py         # background task handlers (optional)
├── exceptions.py
└── constants.py
```

**Discovery is automatic.** `app/modules/registry.py` finds the package and
imports `router`, `models`, `handlers` and `tasks` if present. There is no
import list to update in `api.py`, `migrations/env.py`, `lifespan.py` or
`worker.py` — that was deliberately removed because three of those four lists
failed *silently* when stale.

Do not create a file before it has content. A module with no custom errors does
not need an empty `exceptions.py`.

Order of work: `models.py` → migration → `schemas.py` → `repository.py` →
`service.py` → `dependencies.py` → `router.py` → tests.

---

## 6. Writing code

### Style

- Python 3.14. Full type hints. `async` throughout.
- Ruff enforces 33 rule families including `D` (docstrings) and `ANN`
  (annotations). Run it before claiming done.
- **Every public symbol needs a docstring**, and it should say *why*, not
  restate the signature. `"""Return the user's ID."""` on `get_user_id()` is
  noise; explaining when the ID is absent is not.
- Comments explain **why**, never what. If a comment restates the code, delete
  the comment.

### Suppressions

`# noqa` and `# ty: ignore` need a reason on the same line:

```python
await client.xadd(...)  # ty: ignore[no-matching-overload]
```
with a comment above explaining that redis-py's stubs are narrower than its
runtime behaviour. `unused-ignore-comment` is an error, so a stale suppression
fails the build rather than rotting.

### Async correctness

- Never call blocking I/O in an async function. Use `asyncio.to_thread` — see
  `LocalStorageProvider` for the pattern.
- `ASYNC` lint rules catch the common cases, not all of them.
- The Redis client is bound to the event loop that created it. Anything making a
  new loop must call `close_redis()` first.

---

## 7. Testing

Mirror the source tree. `tests/unit/` for pure logic, `tests/integration/` for
anything needing PostgreSQL or Redis, `tests/modules/<feature>/` for features.

**Test behaviour, not implementation.** Asserting that a mock was called tests
the shape of the code, not its correctness.

**The negative cases matter more.** A token system that accepts valid tokens is
easy; one that rejects forged, expired, cross-typed and stale-version tokens is
the part worth proving.

Autouse fixtures replace the cache, queue, email and metrics singletons with
in-memory fakes, so no test performs real external I/O. The fakes enforce the
same constraints as the real thing — `InMemoryCache` rejects a value Redis could
not store — because a permissive fake makes a test pass and production fail.

Useful fixtures: `session`, `client`, `permissive_client` (returns 500s instead
of re-raising), `authenticated_client`, `principal_factory`, `frozen_time`,
`recorded_metrics`, `storage_root`.

A bug fix comes with a regression test that fails before the fix.

---

## 8. Migrations

```bash
uv run alembic revision --autogenerate -m "description"
```

**Always read the generated migration.** Autogenerate misses renames (it emits a
destructive drop-and-add), server-default changes and CHECK constraints, and it
will happily generate an index creation that locks a production table.

- One logical change per migration.
- `downgrade()` must actually work. Test it.
- Never edit an applied migration; write a new one.
- Destructive changes need two releases: stop using the column, then drop it.
- `CREATE INDEX CONCURRENTLY` on any table with traffic.

`migrations/env.py` respects an explicitly-supplied URL and otherwise reads the
application settings, so the test suite can target `genesis_test` without
touching the development database.

---

## 9. Security rules

- **Never commit a private key.** `keys/` and `*.pem` are gitignored, and
  pre-commit runs `detect-private-key`. A leaked signing key is a full
  authentication bypass, and rotating does not undo it — the key stays in git
  history.
- **Never log a credential.** `SENSITIVE_FIELD_NAMES` drives automatic
  redaction; add to it eagerly.
- **Never interpolate client input into SQL.** Sortable and filterable columns
  come from explicit allow-lists (`app/common/sorting.py`,
  `app/common/filtering.py`).
- **Never return an ORM model directly.** Convert through a response schema, or
  a future column addition ships a password hash.
- **Compare secrets in constant time** — `app/common/utils/crypto.py`.
- **404, not 403, when existence is privileged.** A 403 confirms the resource
  exists.

`APP__DEBUG=true` makes Starlette return full tracebacks in HTTP responses,
bypassing the error handler. The production settings validator refuses to boot
with it on; do not weaken that.

---

## 10. Things that look wrong but are not

Before "fixing" any of these, read the comment next to them:

- **`app/modules/` is empty.** Stage 2 has not started.
- **Lazy imports in `app/infrastructure/observability/`.** Those packages are
  optional extras; a top-level import would crash a deployment without them.
- **`aioboto3` is imported inside a function.** Importing botocore costs about a
  second, which every process would pay even when using local storage.
- **Rate limiting *returns* rather than *raises*.** An exception raised inside a
  `BaseHTTPMiddleware` never reaches the app's exception handlers, so raising
  produces a 500 instead of a 429.
- **`InMemoryCache` round-trips values through JSON.** So it rejects exactly
  what Redis would reject.
- **The outbox keeps exhausted messages.** The payload is often the only record
  of what should have happened.
- **`fileConfig(..., disable_existing_loggers=False)` in `migrations/env.py`.**
  The default silently disables every logger configured before it.
- **CI syncs with `--all-extras` even though the extras are optional.** A type
  checker cannot check an import it cannot resolve, and a test cannot exercise a
  backend that is not installed. Without this, the metrics and tracing paths are
  checked only on machines that happen to have them.
- **`pyproject.toml` declares a `[build-system]` for what looks like an
  application.** Without one, `uv sync` never installs the project, so
  `import app` works only when the process starts in the repository root. Do not
  remove it, and do not "fix" the resulting import by adding `pythonpath` to the
  pytest config — that would paper over the same gap for tests alone.
- **The CI security job exports a requirements file instead of running
  `uvx pip-audit` directly.** `uvx` gives pip-audit its own isolated
  environment, so a bare invocation audits pip-audit's dependencies and passes
  unconditionally.

---

## 11. Reporting your work

- State plainly what you changed and what you verified. If tests fail, say so
  and include the output.
- Never claim something works without running it. "Should work" is not a result.
- If you find a real problem with the requested approach, say so in a sentence
  or two, then deliver the work under a stated assumption.
- Flag anything you left undone, and why.

---

## 12. Key files

| File | Why it matters |
|---|---|
| `app/main.py` | composition root — the whole boot sequence |
| `app/core/settings.py` | every configurable value |
| `app/core/exceptions.py` | the error taxonomy and envelope |
| `app/core/principal.py` | the auth seam Stage 2 plugs into |
| `app/common/dependencies.py` | the DI chain every module copies |
| `app/infrastructure/database/repository.py` | tenant scoping enforcement |
| `app/modules/registry.py` | feature discovery |
| `app/modules/README.md` | the per-file responsibilities of a module |
| `docs/architecture/` | the reasoning behind all of it |
