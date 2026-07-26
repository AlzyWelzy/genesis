# Testing strategy

## Shape

The tree mirrors `app/`. Given a source file, its tests are at the same path
under `tests/`.

```text
tests/
├── conftest.py              root fixtures only
├── unit/                    pure logic, no I/O — milliseconds
├── integration/             real PostgreSQL and Redis
└── modules/                 one directory per feature
```

Most tests should be unit tests, most of the rest integration tests at the
service layer, and only a thin layer of end-to-end tests through HTTP. Not
because of a pyramid diagram, but because of feedback speed and failure
attribution: when a unit test fails it names the function, and when an
end-to-end test fails it names the request.

## What to test at each layer

| Layer | Test | Do not test |
| --- | --- | --- |
| `service.py` | Business rules, edge cases, error paths — **highest value** | HTTP status codes |
| `repository.py` | Query correctness, filtering, pagination, eager loading, **tenant scoping** | Business rules |
| `router.py` | Status codes, response shape, auth enforcement | Business rules again |
| `schemas.py` | Validation boundaries, rejection of bad input | Anything needing a database |

The common mistake is testing business rules through HTTP. Those tests are
slow, and when one fails it does not say which layer broke.

## Non-negotiable tests

Some behaviour is severe enough that it needs a test regardless of coverage
targets:

- **Tenant scoping.** For every tenant-scoped repository: seed two tenants,
  query as one, assert the other's rows are absent. A missed filter returns
  another customer's data with a 200 and nothing in the response to show it.
- **Authorization.** Every protected endpoint, tested with a caller who lacks
  the permission. An endpoint that forgets its dependency passes every
  happy-path test.
- **Error codes.** Assert on `code` and `status_code`, never message text.
  Codes are contract; messages are copy.
- **Regression tests.** Every bug fix ships with a test that fails before it.

## Rules

**No network.** External calls go through the infrastructure protocols;
substitute a fake. A test that hits a real SMTP server is not a test.

**No shared mutable state.** Any test must run alone, and the suite must pass in
any order. `pytest-randomly` shuffles every run to keep us honest — a failure
that only appears under a particular seed is a real ordering bug, not flakiness
to be silenced.

**Rollback, not truncate.** Each database test runs in a transaction that is
rolled back. Faster, and it leaves no ordering dependencies.

**Real PostgreSQL, never SQLite.** They differ on exactly what matters here —
JSONB, arrays, `ON CONFLICT`, constraint and isolation semantics. A suite that
passes on SQLite proves little about production.

**Freeze time, never sleep.** `utc_now()` is the single clock read, which is
what makes this cheap.

**Assert on behaviour, not implementation.** Asserting a mock was called tests
the code's shape, not its correctness. It passes after a refactor that broke the
feature, and fails after a refactor that changed nothing.

## Fixtures

Root `conftest.py` holds only what applies everywhere. Feature fixtures go in
`tests/modules/<feature>/conftest.py` — a fixture added at the root is paid for
by every test in the suite.

Signing keys are generated per session into a temporary directory, so token
tests never depend on a developer having run the keygen script and never sign
with a real key.

## Running

```bash
uv run pytest                    # everything
uv run pytest tests/unit         # fast, no services needed
uv run pytest -m "not integration"
uv run pytest -k billing         # one feature
uv run pytest --cov=app          # with coverage
```

`asyncio_mode = "auto"`, so `async def` tests need no decorator.
`--strict-markers` turns a mistyped marker into an error rather than a silently
unfiltered test.

## Coverage

A useful signal, not a target. High coverage of getters and low coverage of the
service layer is worse than the inverse, and a number gamed to hit a threshold
tells you nothing.

Look at what is uncovered rather than at the percentage. Uncovered error paths
are the ones worth writing tests for — they are the paths that run during an
incident.
