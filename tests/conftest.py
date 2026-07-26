"""Root pytest fixtures.

Why this file exists
--------------------
Fixtures defined here are visible to every test, which makes this file both
powerful and dangerous. It contains only what genuinely applies everywhere:
key material, an application instance and an HTTP client.

Anything feature-specific belongs in ``tests/modules/<feature>/conftest.py``. A
fixture added here is paid for by every test that runs.

Isolation strategy
------------------
Database tests run inside a transaction that is rolled back on teardown.
Rolling back rather than truncating keeps them fast and, more importantly,
order-independent — no test can observe another's writes, so the suite passes
in any order. ``pytest-randomly`` shuffles the order every run to keep us
honest about that.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.security import reset_key_cache


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin async tests to asyncio.

    Without this, anyio-based plugins try to parametrise every async test over
    trio as well, which the application does not support.
    """
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _signing_keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Generate a throwaway Ed25519 key pair for the whole test session.

    Autouse and session-scoped: token tests must never depend on a developer
    having run the key-generation script, and must never sign with a real key.
    Generated once because Ed25519 keygen is cheap but not free.
    """
    from app.core.security import keys as key_module
    from scripts.generate_keys import generate_key_pair

    key_dir = tmp_path_factory.mktemp("keys")
    generate_key_pair(key_dir)

    # `settings` is frozen and already built, so redirect the loader at the
    # throwaway directory rather than trying to mutate configuration.
    original = key_module._read_pem

    def read_from_tmp(path: Path) -> str:
        """Resolve any configured key path against the temporary directory."""
        return original(key_dir / path.name)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(key_module, "_read_pem", read_from_tmp)
    reset_key_cache()
    try:
        yield key_dir
    finally:
        monkeypatch.undo()
        reset_key_cache()


# TODO (Stage 2, needs a running PostgreSQL):
#   database_engine — session-scoped; run the Alembic migrations against a test
#     database so the suite also proves the migrations work.
#   session — function-scoped; open an outer transaction, bind a session to it,
#     roll back on teardown.
#   app / client — build via app.main.create_app() and override the get_session
#     dependency with the transactional session, so writes made through the API
#     are visible to the test and discarded with it.
#   authenticated_client — a client carrying a valid access token.
#   frozen_time — patch app.common.utils.datetime.utc_now, the single clock read.
