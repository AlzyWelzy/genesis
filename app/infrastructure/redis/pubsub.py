"""Publish/subscribe abstraction.

Why this file exists
--------------------
Some things must reach *every* process, not one worker: cache invalidation
after a write, a config reload, a WebSocket fan-out to whichever instance holds
the client's connection. That is broadcast, not queueing, and mixing the two
leads to messages being consumed once when they should have been seen by all.

This module defines the broadcast contract. :mod:`app.infrastructure.queue`
defines the exactly-once work contract. Choosing between them is a design
decision a feature author should make deliberately.

Delivery guarantees
-------------------
Redis pub/sub is **fire and forget**. A subscriber that is down when a message
is published never receives it, and there is no replay. Never use it for
anything that must not be lost — use the queue, or Redis Streams.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Message:
    """A decoded broadcast message.

    The envelope is explicit rather than a bare payload so a consumer can
    reject a message it does not understand instead of crashing on a missing
    key three layers down.

    Attributes:
        channel: Logical channel name, with the namespace prefix stripped.
        name: What happened, e.g. ``"cache.invalidate"``.
        version: Payload schema version. A consumer that does not recognise it
            should skip the message rather than guess.
        payload: The message body.
    """

    channel: str
    name: str
    version: int
    payload: dict[str, Any]


class PubSub(Protocol):
    """Broadcast channel abstraction."""

    async def publish(
        self, channel: str, name: str, payload: dict[str, Any], *, version: int = 1
    ) -> None:
        """Send a message to every current subscriber of ``channel``."""
        ...

    def subscribe(self, *channels: str) -> AsyncIterator[Message]:
        """Yield messages from one or more channels until cancelled."""
        ...


class RedisPubSub:
    """Redis-backed :class:`PubSub` implementation.

    Messages are JSON objects so consumers written in other languages can read
    them.
    """

    async def publish(
        self, channel: str, name: str, payload: dict[str, Any], *, version: int = 1
    ) -> None:
        """Serialise and publish a message.

        Publish failures are logged, not raised. A broadcast is by definition
        something no single caller depends on the outcome of; failing the
        publisher's request because a fan-out notification did not go out is
        the wrong trade.
        """
        from app.infrastructure.redis.client import get_redis

        body = json.dumps({"name": name, "version": version, "payload": payload})
        try:
            await get_redis().publish(build_key("pubsub", channel), body)
        except Exception:  # noqa: BLE001 - broadcast is best-effort
            logger.warning(
                "Publish failed", extra={"channel": channel, "message_name": name}
            )

    async def subscribe(self, *channels: str) -> AsyncIterator[Message]:
        """Subscribe and yield decoded messages until the caller stops.

        Opens a **dedicated connection**. A connection in subscribe mode cannot
        serve normal commands, so borrowing one from the shared pool would
        deadlock every unrelated caller waiting on it.

        Args:
            *channels: Logical channel names; the namespace prefix is added.

        Yields:
            Decoded messages. Undecodable messages are logged and skipped —
            one malformed publisher must not stop a subscriber.
        """
        prefixed = [build_key("pubsub", channel) for channel in channels]
        client: Redis = Redis.from_url(
            str(settings.redis.url),
            socket_timeout=None,  # a subscriber blocks indefinitely by design
            decode_responses=False,
        )
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(*prefixed)
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue  # subscribe/unsubscribe acknowledgements
                message = _decode(raw)
                if message is not None:
                    yield message
        finally:
            await pubsub.aclose()
            await client.aclose()


def _decode(raw: dict[str, Any]) -> Message | None:
    """Decode one raw Redis message, returning ``None`` when unusable."""
    channel = raw["channel"]
    channel_name = (
        channel.decode() if isinstance(channel, bytes) else str(channel)
    ).removeprefix(build_key("pubsub") + ":")

    try:
        body = json.loads(raw["data"])
        return Message(
            channel=channel_name,
            name=body["name"],
            version=body.get("version", 1),
            payload=body.get("payload", {}),
        )
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        logger.warning("Skipping undecodable message", extra={"channel": channel_name})
        return None


#: Process-wide publisher.
pubsub: PubSub = RedisPubSub()

# TODO: replace with Redis Streams wherever replay matters — Streams keep a log
# a late subscriber can read from, which plain pub/sub cannot.
