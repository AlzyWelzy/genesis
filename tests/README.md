# Tests

The test tree mirrors the application tree. Given a source file, its tests are
at the same path under `tests/` — no searching, no convention to memorise.

```
tests/
├── conftest.py              # root fixtures: settings, engine, app, client
├── unit/                    # pure logic, no I/O
│   ├── common/              # mirrors app/common/
│   └── core/                # mirrors app/core/
├── integration/             # real PostgreSQL, real transactions
│   └── infrastructure/      # mirrors app/infrastructure/
└── modules/                 # mirrors app/modules/ — one dir per feature
```

## Where a feature's tests go

Everything for a feature lives in `tests/modules/<feature>/`, mirroring the
module's own files:

```
tests/modules/billing/
├── conftest.py              # fixtures for this feature only
├── test_service.py          # business rules — the tests that matter most
├── test_repository.py       # queries, against a real database
├── test_router.py           # status codes, payload shape, auth
└── test_schemas.py          # validation edge cases
```

Never put a feature's fixtures in the root `conftest.py`. It is loaded for every
test in the suite; a fixture used by one module slows down and couples all of
them.

## What to test at each layer

| Layer | Test | Do not test |
| --- | --- | --- |
| `service.py` | Business rules, edge cases, error paths. **Highest value.** | HTTP status codes |
| `repository.py` | Query correctness, filtering, pagination, eager loading | Business rules |
| `router.py` | Status codes, response shape, auth enforcement | Business rules again |
| `schemas.py` | Validation boundaries, rejection of bad input | Anything requiring a database |

The common failure is testing business rules through HTTP. Those tests are slow,
and when one fails it does not tell you which layer broke.

## Database tests

Use a **real PostgreSQL** instance, not SQLite. They differ on the things that
matter here — JSONB, arrays, `ON CONFLICT`, constraint semantics, transaction
isolation — so a suite that passes on SQLite proves little about production.

Each test runs in a transaction that is **rolled back** afterwards. Rollback,
not truncation: it is faster and leaves no ordering dependencies between tests.

## Rules

- **No network.** External calls go through the infrastructure abstractions;
  substitute a fake implementation. A test that hits a real SMTP server is not
  a test.
- **No shared mutable state between tests.** Any test must be runnable alone,
  and the suite must pass in a random order.
- **Freeze time** rather than sleeping. `app.common.utils.datetime.utc_now` is
  the single clock read, which is what makes this cheap.
- **Assert on behaviour, not implementation.** Asserting a mock was called is
  usually a test of the code's shape, not its correctness.
- **A bug fix comes with a regression test** that fails before the fix.

## Running

```bash
uv run pytest                     # everything
uv run pytest tests/unit          # fast, no database needed
uv run pytest -k billing          # one feature
uv run pytest --cov=app           # with coverage
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so `async def` tests need no
decorator.
