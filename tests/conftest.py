"""Root pytest fixtures.

Why this file exists
--------------------
Fixtures defined here are visible to every test in the suite, which makes this
file both powerful and dangerous. It should contain only what genuinely applies
everywhere: the application instance, the test database and the HTTP client.

Anything feature-specific belongs in ``tests/modules/<feature>/conftest.py``.
A fixture added here is imported (and its cost paid) by every test that runs.

Isolation strategy
------------------
Each test runs inside a transaction that is rolled back on teardown. Rolling
back rather than truncating keeps tests fast and, more importantly, order
independent — no test can observe another's writes, so the suite passes in any
order and in parallel.
"""

from collections.abc import AsyncIterator

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin async tests to asyncio.

    Without this, anyio-based plugins attempt to parametrise every async test
    over trio as well, which the application does not support.
    """
    return "asyncio"


@pytest.fixture(scope="session")
def test_settings() -> object:
    """Build a ``Settings`` instance pointed at test infrastructure.

    Constructed directly rather than read from ``.env`` so a developer's local
    configuration can never cause a test run to touch a real database.
    """
    raise NotImplementedError(
        "Construct app.core.settings.Settings(...) with a dedicated test "
        "database URL and the console email provider."
    )


@pytest.fixture(scope="session")
async def database_engine() -> AsyncIterator[object]:
    """Create the test database schema once per session.

    Schema creation is expensive; doing it per test would dominate the run.
    Prefer running the Alembic migrations here over ``metadata.create_all`` —
    that way the suite also verifies the migrations actually work.
    """
    raise NotImplementedError


@pytest.fixture
async def session(database_engine: object) -> AsyncIterator[object]:
    """Yield a session wrapped in a transaction rolled back after the test.

    Opens an outer transaction, binds the session to it, and rolls back on
    teardown so nothing the test wrote survives.
    """
    raise NotImplementedError


@pytest.fixture
def app(test_settings: object) -> object:
    """Build the FastAPI application with test settings applied.

    Uses :func:`app.main.create_app` rather than importing the module-level
    ``app``, so each test session gets an instance built against the test
    configuration.
    """
    raise NotImplementedError


@pytest.fixture
async def client(app: object, session: object) -> AsyncIterator[object]:
    """Yield an ``httpx.AsyncClient`` bound to the app via ASGITransport.

    Overrides the ``get_session`` dependency with the transactional test
    session, so writes made through the API are visible to the test and rolled
    back with it. No network socket is involved.
    """
    raise NotImplementedError


# TODO: add an `authenticated_client` fixture once an auth module exists.
# TODO: add a `frozen_time` fixture patching app.common.utils.datetime.utc_now.
# TODO: add fake Cache / EmailProvider / Queue fixtures so no test can perform
# real I/O against an external system.
