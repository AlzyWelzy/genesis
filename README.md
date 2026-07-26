# Genesis

A production-grade FastAPI backend, structured feature-first to stay navigable
as it grows to hundreds of endpoints.

**Scaffolding only** — the architecture is complete, no business logic exists
yet.

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2.x (async) · PostgreSQL · Alembic ·
Pydantic v2 · PyJWT (Ed25519) · pwdlib (Argon2) · uv · Ruff · ty · pytest

## Setup

```bash
uv sync                                     # install dependencies
cp .env.example .env                        # configure
uv run python scripts/generate_keys.py      # generate JWT signing keys
uv run alembic upgrade head                 # apply migrations (none yet)
uv run python main.py                       # http://localhost:8000/docs
```

`DATABASE__URL` is the only required setting.

## Commands

```bash
uv run python main.py            # dev server with reload
uv run pytest                    # tests
uv run pytest tests/unit         # fast tests, no database
uv run ruff check --fix .        # lint
uv run ruff format .             # format
uv run ty check                  # type check
uv run alembic revision --autogenerate -m "..."
```

## Layout

```text
app/
├── main.py             # bootstrap — the only composition root
├── api.py              # root router; every feature router is included here
├── core/               # settings, security, logging, lifespan, middleware, errors
├── infrastructure/     # database, redis, storage, email, queue — all behind interfaces
├── common/             # pagination, responses, enums, types, pure utilities
├── events/             # domain events and the in-process bus
└── modules/            # business features, one package each
migrations/             # Alembic
scripts/                # operational entry points
tests/                  # mirrors app/
docs/                   # architecture and decisions
```

## The rules

```text
Router → Dependency → Service → Repository → Infrastructure → PostgreSQL
```

- Dependencies point inward. `core`, `common` and `infrastructure` never import
  from `modules`.
- Routers own HTTP. Services own business rules. Repositories own SQL. Nothing
  crosses two layers.
- Services never import `fastapi` and never raise `HTTPException` — they raise
  `AppError` subclasses, and the global handlers translate them.
- The service commits; repositories never do. That is what makes several writes
  one transaction.
- Configuration is read once, in `app/core/config.py`. Nothing else touches
  `os.environ`.

Full rationale in [`docs/architecture.md`](docs/architecture.md). How to build a
feature: [`app/modules/README.md`](app/modules/README.md).

## Adding a feature

Create `app/modules/<feature>/`, then work outward: `models.py` →
`schemas.py` → `repository.py` → `service.py` → `dependencies.py` →
`router.py`. Include the router in [`app/api.py`](app/api.py), import the models
in [`migrations/env.py`](migrations/env.py), generate a migration, and add tests
under `tests/modules/<feature>/`.

## Security notes

- `keys/` is gitignored. A committed private key means anyone can mint a token
  for any user — and rotating is not sufficient, since the key stays in git
  history.
- Every environment gets its own key pair; production keys come from a secret
  manager.
- `.env` is gitignored. `.env.example` documents the keys with no real values.
