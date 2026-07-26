# Genesis

A production-grade FastAPI backend, structured feature-first to stay navigable
as it grows to hundreds of endpoints.

**Stage 1 (Foundation) is complete.** The framework, conventions and operational
surface are in place; no business features exist yet. See
[the roadmap](#roadmap).

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2.x (async) · PostgreSQL 17 · Alembic ·
Pydantic v2 · PyJWT (Ed25519) · pwdlib (Argon2id) · Redis · uv · Ruff · ty ·
pytest · Docker

## Quick start

```bash
uv sync                                     # install dependencies
cp .env.example .env                        # configure
uv run python scripts/generate_keys.py      # generate JWT signing keys
docker compose up -d postgres redis         # start dependencies
uv run alembic upgrade head                 # apply migrations (none yet)
uv run python main.py                       # http://localhost:8000/docs
```

Or run everything in containers:

```bash
docker compose up
```

`DATABASE__URL` is the only required setting.

## Commands

```bash
uv run python main.py            # dev server with reload
uv run pytest                    # tests (67 passing)
uv run pytest tests/unit         # fast tests, no services needed
uv run ruff check --fix .        # lint (33 rule families)
uv run ruff format .             # format
uv run ty check                  # type check
uv run pre-commit install        # install git hooks
uv run alembic revision --autogenerate -m "..."
```

## Layout

```text
app/
├── main.py             composition root — builds the app, nothing else
├── api.py              root router; feature routers are included here
├── core/               settings, context, security, logging, lifespan,
│                       middleware, exceptions, openapi
├── infrastructure/     database, redis, storage, email, queue, observability
├── common/             pagination, sorting, filtering, responses, DI, utils
├── events/             domain events and the in-process bus
├── system/             health probes and JWKS — operational, not business
└── modules/            business features, one package each (empty)
migrations/             Alembic
scripts/                operational entry points
tests/                  mirrors app/
docs/architecture/      the project's constitution
```

## The rules

```text
Router → Dependency → Service → Repository → Infrastructure → PostgreSQL
```

- **Dependencies point inward.** `core`, `common` and `infrastructure` never
  import from `modules`. Enforced by `scripts/check_dependency_rule.sh` in
  pre-commit and CI.
- **Routers own HTTP. Services own business rules. Repositories own SQL.**
  Nothing crosses two layers.
- **Services never import `fastapi`** and never raise `HTTPException` — they
  raise `AppError` subclasses, and global handlers translate them.
- **The service commits; repositories never do.** That is what makes several
  writes one transaction.
- **Every tenant-scoped query goes through `TenantRepository`,** which applies
  the tenant filter structurally rather than by convention.
- **Configuration is read once**, in `app/core/config.py`. Nothing else touches
  `os.environ`.

Full rationale in [`docs/architecture/`](docs/architecture/). Start with
[`dependency-rules.md`](docs/architecture/dependency-rules.md); everything else
follows from it.

## Key decisions

Recorded with their rejected alternatives in
[`docs/architecture/adr/`](docs/architecture/adr/).

| Decision | Choice |
| --- | --- |
| Architecture | Layered, feature-first, dependencies inward |
| API | `snake_case`, enveloped responses, URL versioning (`/api/v1`) |
| Primary keys | UUIDv7, generated in Python |
| Multi-tenancy | Row-level `tenant_id`, enforced in the repository base |
| Tokens | Ed25519 with `kid` rotation, JWKS, 15-min access + token versioning |
| Passwords | Argon2id, length-based policy, transparent rehashing |

## Operational surface

| Endpoint | Purpose |
| --- | --- |
| `/live` | Liveness — checks nothing external, by design |
| `/ready` | Readiness — checks the database; Redis is non-fatal |
| `/health` | Human-readable summary for dashboards |
| `/.well-known/jwks.json` | Public keys for token verification |
| `/docs` | OpenAPI UI (disabled via `APP__ENABLE_DOCS`) |

Every response carries `X-Request-ID` and `X-Correlation-ID`; every error body
carries the request ID so support can find the log line.

## Adding a feature

Create `app/modules/<feature>/`, then work outward: `models.py` → `schemas.py`
→ `repository.py` → `service.py` → `dependencies.py` → `router.py`. Include the
router in [`app/api.py`](app/api.py), import the models in
[`migrations/env.py`](migrations/env.py), generate a migration, and add tests
under `tests/modules/<feature>/`.

Per-file responsibilities: [`app/modules/README.md`](app/modules/README.md).

## Roadmap

- **Stage 1 — Foundation** ✅ structure, tooling, configuration, logging, errors,
  security, database, migrations, lifespan, middleware, API conventions, DI,
  plus health endpoints, observability seams, Docker and CI.
- **Stage 2 — Platform** authentication, authorization, users, organizations,
  events, audit, background jobs, notifications, storage, cache, search,
  feature flags.
- **Stage 3 — Product** the business modules.
- **Stage 4 — Operations** deployment, monitoring, runbooks.

## Security notes

- `keys/` and `*.pem` are gitignored. A committed private key is a full
  authentication bypass, and reverting the commit does not undo it.
- Every environment gets its own key pair; production keys come from a secret
  manager, not from `scripts/generate_keys.py` on a laptop.
- `.env` is gitignored. Set `SECRETS_DIR=/run/secrets` in deployment to read
  file-mounted secrets instead — no code change required.
- The settings validator refuses to start with an unsafe production
  configuration. See [`configuration.md`](docs/architecture/configuration.md).
