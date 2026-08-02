"""``CollectingEmailProvider`` must apply the same guards as a real transport.

Suppression and idempotency are not transport concerns — they decide whether a
message is sent at all. A fake that skips them lets a test assert an email was
sent to an address the real provider would refuse.
"""

import pytest

from app.infrastructure.email.client import EmailMessage
from app.infrastructure.email.providers import (
    CollectingEmailProvider,
    ConsoleEmailProvider,
)
from app.infrastructure.redis.client import build_key, close_redis, init_redis

pytestmark = pytest.mark.integration

#: The example template renders under ``StrictUndefined``, so every variable it
#: references must be supplied — a missing one is an error rather than a blank,
#: which is the point of that setting.
CONTEXT = {
    "subject": "parity",
    "app_name": "Genesis",
    "recipient_name": "Someone",
    "body": "Body text.",
    "action_label": "Open",
    "action_url": "https://example.com",
}


@pytest.fixture
async def implementations():
    try:
        client = await init_redis()
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - absence of Redis is a skip
        pytest.skip(f"Redis unavailable: {exc}")

    async def clear() -> None:
        async for key in client.scan_iter(match=build_key("email:sent") + "*"):
            await client.delete(key)

    await clear()
    collecting = CollectingEmailProvider()
    yield collecting, ConsoleEmailProvider()
    collecting.clear()
    await clear()
    await close_redis()


class TestIdempotency:
    async def test_a_duplicate_is_suppressed_by_both(self, implementations) -> None:
        """The guard is the reason a retried job does not mail twice."""
        collecting, _ = implementations
        message = EmailMessage(
            to=["a@example.com"],
            subject="parity",
            template="example",
            context=CONTEXT,
        )

        await collecting.send(message)
        await collecting.send(message)

        assert len(collecting.sent) == 1

    async def test_a_distinct_message_is_not_suppressed(self, implementations) -> None:
        """Deduplication must not become a general-purpose drop."""
        collecting, _ = implementations

        for subject in ("one", "two"):
            await collecting.send(
                EmailMessage(
                    to=["a@example.com"],
                    subject=subject,
                    template="example",
                    context=CONTEXT | {"subject": subject},
                )
            )

        assert len(collecting.sent) == 2

    async def test_neither_provider_raises_on_a_duplicate(
        self, implementations
    ) -> None:
        """Suppression is not an error; the caller did nothing wrong."""
        collecting, console = implementations
        message = EmailMessage(
            to=["b@example.com"],
            subject="parity",
            template="example",
            context=CONTEXT,
        )

        for provider in (collecting, console):
            await provider.send(message)
            await provider.send(message)
