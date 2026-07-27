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


## Property-based tests

`tests/property/` is a separate layer from the example tests, not a duplicate of
them. The distinction is who chooses the input.

An example test asserts behaviour for an input the author picked. That is
exactly where its blind spot is: an author who had thought of the failing input
would have fixed the bug rather than written a test that passes. Four bugs in
this repository shipped through a green example suite for that reason:

| Bug | Why every example passed |
|---|---|
| Cursor rejected ~7% of the time | The signature's raw bytes only sometimes contained the delimiter |
| `deep_merge` aliased its inputs | The test asserted "does not mutate inputs", which was true |
| `to_payload` could not encode a `date` | No test carried one |
| `timedelta` lost microseconds | Only past ~272 years of duration |

A property test states an invariant that must hold for **every** input, and
Hypothesis searches for a counterexample — reaching lone surrogates, empty
strings, hundred-thousand-day durations and values differing only by Unicode
normalisation form. On failure it shrinks to the smallest input that still
breaks, which usually makes the cause obvious.

### Profiles

| Profile | Examples | Used for |
|---|---|---|
| `dev` (default) | 50 | Local runs; keeps the suite fast enough to run always |
| `ci` | 500 | What CI enforces |
| `thorough` | 5000 | Pre-release, or when a bug suggests a class of input was never explored |

Select with `HYPOTHESIS_PROFILE=ci` or `--hypothesis-profile=thorough`.

### When to write one

Whenever you add a pure function that encodes, parses, truncates, escapes,
merges or signs. Good properties are relationships, not restatements:

* **Round-trip** — `decode(encode(x)) == x` for every `x`, and `decode` rejects
  everything `encode` did not produce.
* **Bounds** — `len(truncate(s, n)) <= n`.
* **Idempotence** — `slugify(slugify(x)) == slugify(x)`.
* **Independence** — the result shares no mutable structure with its inputs.

Keep I/O out. Hypothesis calls a test hundreds of times; anything touching
PostgreSQL or Redis belongs in `tests/integration/`.


## Concurrency tests

`tests/concurrency/` is the third layer, and it reaches what neither of the
others can. Example tests fix the input; property tests generate the input;
neither can express *two things happening at once*.

That matters because every guarantee this platform makes has a concurrent
clause, and the clause is the whole point of the mechanism:

| Mechanism | The guarantee | Sequentially |
|---|---|---|
| `FOR UPDATE SKIP LOCKED` | Two relays share the outbox | Passes with or without it |
| Consumer group | Two workers split a stream | Passes either way |
| `SET NX` | One winner per idempotency key | Passes either way |
| Version column | The loser refetches | Passes either way |
| Blocking pool | A burst waits, not bypasses | Passes either way |

Two bugs surfaced here that nothing else could have found:

**The Redis pool failed open under load.** `ConnectionPool` raises
`MaxConnectionsError` the moment it is exhausted, and every Redis-backed control
in this codebase fails open by design — the rate limiter admits, the email guard
sends, the cache misses. So beyond `max_connections` concurrent operations they
all stopped working *together*. For the rate limiter that is not degradation but
inversion: an attacker needs only enough concurrency to drain the pool, and the
limit stops applying exactly when it exists to apply. Now a
`BlockingConnectionPool`, which applies backpressure — visible as latency rather
than silent in a log.

**Every idle worker poll raised.** `XREADGROUP` blocked for 5000ms while the
client's socket deadline was 5.0s, so the client gave up first on *every* poll of
an empty queue. The worker logged a full traceback and slept, forever. Invisible
to any test that enqueues a job first, because then the read returns immediately
and never reaches its block duration.

### Writing them

* **Separate sessions and clients.** Work sharing one session is serialised by
  it, so the test passes on code that would deadlock or double-write.
* **`asyncio.Event`, not `sleep`,** when a race needs a specific interleaving. A
  sleep is a coin flip that passes on a fast machine and fails in CI.
* **Real PostgreSQL and Redis.** The guarantees under test are theirs; a fake
  asserts only that we called the methods we thought we called.
* **Exceed the pool size.** Below `max_connections`, every Redis test passes on a
  broken pool — which is exactly how that bug survived.
