"""Root pytest fixtures.

Why this file exists
--------------------
Fixtures defined here are visible to every test in the suite, which makes this
file both powerful and dangerous. It contains only what genuinely applies
everywhere: the application instance, the test database, the HTTP client, and
the fakes that keep unit tests free of I/O.

Anything feature-specific belongs in ``tests/modules/<feature>/conftest.py``.
A fixture added here is imported — and its cost paid — by every test that runs.

Isolation strategy
------------------
Each database test runs inside a transaction that is rolled back on teardown.
Rolling back rather than truncating keeps tests fast and, more importantly,
order-independent: no test can observe another's writes, so the suite passes in
any order and under ``pytest-randomly``.

No network
----------
Autouse fixtures replace the cache, queue, email and metrics singletons with
in-memory fakes for every test. A test that reaches a real external system is
not a test — it is a monitoring check with a misleading name.
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest

#: Set from ``TEST_DATABASE_URL`` when present, so CI can point at its own
#: service container without editing anything.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/genesis_test",
)

# Point the *application's* settings at the test database, before anything
# imports them.
#
# This has to happen here, above the app imports, because
# `app.infrastructure.database.session` builds its engine at import time from
# `settings.database.url`. Without this the local `.env` wins and that
# module-level engine connects to the **development** database — so any test
# exercising code that opens its own `session_scope()` (the outbox relay, a
# queue handler, a script) reads and writes a developer's real data. It shows
# up first as a baffling "relation does not exist", and the benign version of
# that is the lucky one.
#
# `setdefault`, so an explicit DATABASE__URL — CI's service container — still
# wins.
os.environ.setdefault("DATABASE__URL", TEST_DATABASE_URL)

from app.infrastructure.email.providers import (  # noqa: E402 - must follow the env setup
    CollectingEmailProvider,
    set_email_provider,
)
from app.infrastructure.observability.metrics import (  # noqa: E402 - see above
    NoOpMetrics,
    RecordingMetrics,
    set_metrics,
)
from app.infrastructure.queue.client import (  # noqa: E402 - see above
    InMemoryQueue,
    set_queue,
)
from app.infrastructure.redis.cache import (  # noqa: E402 - see above
    InMemoryCache,
    set_cache,
)

#: Resolved at import so the async fixture performs no filesystem I/O.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

#: Marks tests that need a real PostgreSQL. Collected but skipped when the
#: database is unreachable, so `pytest` on a laptop with nothing running still
#: reports honestly rather than erroring.
requires_db = pytest.mark.integration


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin async tests to asyncio.

    Without this, anyio-based plugins parametrise every async test over trio as
    well, which the application does not support.
    """
    return "asyncio"


# ---------------------------------------------------------------------------
# Isolation from external systems
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_cache() -> Iterator[InMemoryCache]:
    """Replace the process-wide cache with an in-memory one.

    Autouse: a test that accidentally hits Redis passes locally and fails in CI,
    which is the worst of both worlds. Making isolation the default removes the
    choice.
    """
    cache = InMemoryCache()
    set_cache(cache)
    yield cache
    cache.clear()


@pytest.fixture(autouse=True)
def fake_queue() -> Iterator[InMemoryQueue]:
    """Replace the process-wide queue with a collecting one.

    Lets a test assert that a job *would* have been enqueued, and with what
    payload, without a worker or a broker.
    """
    queue = InMemoryQueue()
    set_queue(queue)
    yield queue
    queue.clear()


@pytest.fixture(autouse=True)
def fake_email() -> Iterator[CollectingEmailProvider]:
    """Replace the email provider with one that captures messages."""
    provider = CollectingEmailProvider()
    set_email_provider(provider)
    yield provider
    provider.clear()


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    """Keep metrics inert unless a test opts into recording them."""
    set_metrics(NoOpMetrics())
    yield
    set_metrics(NoOpMetrics())


@pytest.fixture
def recorded_metrics() -> Iterator[RecordingMetrics]:
    """Install a recording metrics backend for assertions."""
    recorder = RecordingMetrics()
    set_metrics(recorder)
    yield recorder
    recorder.clear()
    set_metrics(NoOpMetrics())


# ---------------------------------------------------------------------------
# Signing keys
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _signing_keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Generate a throwaway key pair for the whole session.

    Generated rather than committed: a fixture key checked into a repository
    eventually gets copied into a real deployment, and Argon2/Ed25519 key
    generation is fast enough that there is no reason to take the risk.

    Session-scoped because key generation, while fast, is not free per test.
    """
    from app.core.security.keys import reset_key_cache
    from scripts.generate_keys import generate_key_pair

    keys_dir = tmp_path_factory.mktemp("keys")
    generate_key_pair(keys_dir, force=True)

    from app.core.config import settings

    original_private = settings.jwt.private_key_path
    original_public = settings.jwt.public_key_path
    # `Settings` is frozen, so reach past Pydantic's guard rather than
    # rebuilding the whole object graph for a path swap.
    object.__setattr__(settings.jwt, "private_key_path", keys_dir / "private.pem")
    object.__setattr__(settings.jwt, "public_key_path", keys_dir / "public.pem")
    reset_key_cache()

    yield

    object.__setattr__(settings.jwt, "private_key_path", original_private)
    object.__setattr__(settings.jwt, "public_key_path", original_public)
    reset_key_cache()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """Bring the test schema to head once per session.

    Runs the **real Alembic migrations**, not ``metadata.create_all``. That
    difference is the point: ``create_all`` builds the schema the models
    describe, so it can only ever agree with them. Running the migrations
    verifies the migrations *also* produce that schema — the bug nothing else
    catches, and the one that breaks a deploy after every test has passed.

    Synchronous, and deliberately so. Alembic drives its own event loop, and
    this fixture holds no async resources, so there is nothing here bound to a
    loop that a later test would find closed.

    Yields:
        The database URL, so dependent fixtures cannot forget to depend on it.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    # The migration run doubles as the connectivity probe: no database means no
    # migration, and a skip is the honest outcome. Adding a second driver just
    # to ask "are you there?" would be a dependency for nothing.
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    except Exception as exc:  # noqa: BLE001 - absence of a database is a skip
        pytest.skip(f"PostgreSQL unavailable at {TEST_DATABASE_URL}: {exc}")

    yield TEST_DATABASE_URL

    command.downgrade(config, "base")


@pytest.fixture
async def database_engine(migrated_database: str) -> AsyncIterator[object]:
    """An async engine bound to *this test's* event loop.

    Function-scoped on purpose. asyncpg connections are bound to the loop that
    created them, and pytest-asyncio gives every test a fresh loop — so a
    session-scoped engine hands the second test a connection attached to a loop
    that no longer exists, which surfaces as "attached to a different loop".

    Creating an engine is cheap: it opens no connections until one is used.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_database, poolclass=None)
    yield engine
    await engine.dispose()


@pytest.fixture
async def shared_engine(migrated_database: str) -> AsyncIterator[None]:
    """Make the module-level engine usable from *this* test's event loop.

    Most database tests take the ``session`` fixture, which builds its own
    engine and rolls back. That does not work for anything exercising code
    which opens its own ``session_scope()`` — the outbox relay, a queue handler,
    a script — because the relay must see *committed* rows, and a session it did
    not create cannot give it those.

    Such code reaches ``app.infrastructure.database.session.engine``, created at
    import time and bound to whichever loop first used it. pytest-asyncio gives
    every test a fresh loop, so the second test to touch it fails deep inside
    asyncpg with "Event loop is closed" — a traceback that says nothing about
    the actual cause.

    Disposing on both sides forces the pool to reconnect on the current loop.

    Tests using this fixture are responsible for their own cleanup: they commit,
    so nothing is rolled back for them.
    """
    from app.infrastructure.database.session import dispose_engine

    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
async def session(database_engine: object) -> AsyncIterator[object]:
    """Yield a session inside a transaction that is rolled back afterwards.

    The outer transaction is owned by this fixture and never committed, so
    anything the test wrote — including through a service that called
    ``commit()`` — disappears on teardown. That is what makes the suite order
    independent: no test can observe another's writes, so it passes under
    ``pytest-randomly`` and in parallel.

    Rollback rather than truncation, because rollback is both faster and
    complete: truncation has to name every table and silently misses any added
    later.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    connection = await database_engine.connect()  # ty: ignore[unresolved-attribute]
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False)
    db_session = factory()

    try:
        yield db_session
    finally:
        await db_session.close()
        await transaction.rollback()
        await connection.close()


# ---------------------------------------------------------------------------
# Application and HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> Iterator[object]:
    """Build the application as a deployed instance would be configured.

    Two deliberate differences from simply importing ``app.main.app``:

    **The lifespan does not run.** It connects to PostgreSQL and Redis and
    refuses to start without the former. Most tests want the routes and
    middleware, not the resources; the tests that care about startup exercise
    it explicitly.

    **Debug is forced off.** Starlette's ``ServerErrorMiddleware`` returns a
    full traceback as the response body when ``debug=True``, bypassing the
    application's exception handler entirely. The local ``.env`` sets
    ``APP__DEBUG=true`` for convenience, so without this override the suite
    would assert against behaviour no deployed environment ever exhibits — and
    would not notice if the sanitised envelope broke.
    """
    from app.core.config import settings
    from app.main import create_app

    original = settings.app.debug
    object.__setattr__(settings.app, "debug", False)
    try:
        yield create_app()
    finally:
        object.__setattr__(settings.app, "debug", original)


@pytest.fixture
async def client(app: object) -> AsyncIterator[object]:
    """Yield an ``httpx.AsyncClient`` bound to the app in-process.

    Uses ``ASGITransport``, so no socket is opened and no port can conflict
    when tests run in parallel.
    """
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),  # ty: ignore[invalid-argument-type]
        base_url="http://testserver",
    ) as http_client:
        yield http_client


@pytest.fixture
async def permissive_client(app: object) -> AsyncIterator[object]:
    """A client that returns 500 responses instead of re-raising.

    ``ASGITransport`` re-raises unhandled application exceptions by default,
    which is the right default: an unexpected error in a test should surface as
    that error, not as a confusing assertion about a status code.

    Tests that deliberately assert on the sanitised 500 envelope need the
    opposite, so they take this fixture explicitly — the exception is visible in
    the test's signature rather than weakening every other test.
    """
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),  # ty: ignore[invalid-argument-type]
        base_url="http://testserver",
    ) as http_client:
        yield http_client


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """A throwaway root for the local storage provider."""
    root = tmp_path / "storage"
    root.mkdir()
    return root


@pytest.fixture
def principal_factory():
    """Build a stub principal satisfying :class:`~app.core.principal.Principal`.

    Structural typing means this needs no ``User`` model and no import from a
    feature module, so authorization can be tested in full before Stage 2
    exists — and the tests keep working unchanged once it does.
    """

    @dataclass(frozen=True, slots=True)
    class StubPrincipal:
        id: UUID = field(default_factory=uuid7)
        is_active: bool = True
        token_version: int = 0
        permissions: frozenset[str] = frozenset()
        is_superuser: bool = False

    return StubPrincipal


@pytest.fixture
def registered_principal(principal_factory):
    """Register a loader returning one principal, and clean up afterwards.

    The seam is process-wide global state, so leaving it registered would leak
    an authenticated identity into every test that ran afterwards.
    """
    from app.core.principal import (
        reset_principal_seams,
        set_membership_checker,
        set_principal_loader,
    )

    principal = principal_factory()

    async def load(_subject: str):
        return principal

    async def is_member(_principal, _tenant_id) -> bool:
        return True

    set_principal_loader(load)
    set_membership_checker(is_member)
    yield principal
    reset_principal_seams()


@pytest.fixture
async def authenticated_client(app, registered_principal):
    """An HTTP client presenting a valid access token for a known principal.

    Mints a real token with the application's own signing key and sends it in
    the ``Authorization`` header, so requests traverse the genuine
    verification path rather than a bypassed dependency override. A test that
    overrides the auth dependency proves the endpoint works when
    authentication is skipped, which is not the thing worth proving.
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.security import create_access_token

    token = create_access_token(
        str(registered_principal.id),
        token_version=registered_principal.token_version,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
def frozen_time(monkeypatch):
    """Freeze the clock, and let a test advance it deliberately.

    Every clock read in the application goes through
    :func:`app.common.utils.datetime.utc_now`, which is precisely what makes
    this possible with one patch instead of many.

    Freezing beats sleeping: a test that sleeps is slow, flaky under load, and
    cannot exercise a 30-day expiry at all.

    Usage::

        def test_token_expires(frozen_time):
            token = create_access_token("u")
            frozen_time.advance(timedelta(hours=1))
            with pytest.raises(InvalidTokenError):
                decode_token(token)
    """

    class Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        def advance(self, delta: timedelta) -> datetime:
            """Move the clock forward."""
            self.now += delta
            return self.now

        def set(self, moment: datetime) -> datetime:
            """Jump the clock to an absolute time."""
            self.now = moment
            return self.now

    clock = Clock()
    # Patched at each import site: `from ... import utc_now` binds the function
    # into the importing module's namespace, so patching only the source module
    # would leave those bindings pointing at the real clock.
    for module in ("app.common.utils.datetime", "app.events.base"):
        monkeypatch.setattr(f"{module}.utc_now", lambda: clock.now, raising=False)
    return clock
