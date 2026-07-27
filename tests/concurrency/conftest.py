"""Fixtures for concurrent integration tests."""

import pytest

from app.infrastructure.outbox.models import OutboxMessage
from app.infrastructure.queue.client import (
    DEAD_LETTER_KEY,
    DELAYED_KEY,
    STREAM_KEY,
)
from app.infrastructure.redis.client import build_key, close_redis, init_redis


@pytest.fixture
async def live_redis():
    """A real Redis with the queue keys cleared, skipping when unavailable."""
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    async def clear() -> None:
        for key in (STREAM_KEY, DELAYED_KEY, DEAD_LETTER_KEY):
            await client.delete(build_key(key))
        for pattern in ("jobs:idem", "email:sent", "ratelimit", "bucket", "lock"):
            async for key in client.scan_iter(match=build_key(pattern) + "*"):
                await client.delete(key)

    await clear()
    yield client
    await clear()
    await close_redis()


@pytest.fixture
async def empty_outbox(shared_engine):
    """An empty ``outbox_messages`` table, committed.

    Uses ``shared_engine`` because the relay opens its own ``session_scope``:
    a row staged inside the rollback-per-test ``session`` fixture is never
    committed, so the relay cannot see it — which is the whole point of the
    mechanism under test.
    """
    from sqlalchemy import delete

    from app.infrastructure.database.session import session_scope

    async def clear() -> None:
        async with session_scope() as session:
            await session.execute(delete(OutboxMessage))

    await clear()
    yield
    await clear()
