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
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

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
        body = json.dumps({"name": name, "version": version, "payload": payload})
        try:
            await get_redis().publish(build_key("pubsub", channel), body)
        except Exception:  # noqa: BLE001 - broadcast is best-effort
            logger.warning(
                "Publish failed", extra={"channel": channel, "message_name": name}
            )

    async def subscribe(self, *channels: str) -> AsyncGenerator[Message]:
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
    except json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError:
        logger.warning("Skipping undecodable message", extra={"channel": channel_name})
        return None


#: Process-wide publisher.
pubsub: PubSub = RedisPubSub()


class RedisStreamsPubSub:
    """Broadcast backed by Redis Streams, for messages that must not be lost.

    When to prefer this over :class:`RedisPubSub`
    ---------------------------------------------
    Plain pub/sub is fire-and-forget: a subscriber that is down when a message
    is published never learns it happened, and there is no way to find out
    afterwards. That is fine for cache invalidation — the next write republishes
    — and unacceptable for anything a consumer must eventually observe.

    Streams keep an append-only log. A subscriber that reconnects reads from
    where it left off, so a deploy, a crash or a network blip costs latency
    rather than data.

    The cost
    --------
    The log is retained, so it consumes memory. ``maxlen`` caps it: entries
    beyond the cap are trimmed, which means a subscriber offline for longer
    than the retention window still misses messages. Streams turn "lost on
    disconnect" into "lost after N messages", which is a much better guarantee
    but is not an unbounded one. Anything requiring true durability belongs in
    a database, not a broadcast channel.

    Approximate trimming (``~``) is used because exact trimming forces Redis to
    scan, and the difference between 10,000 and 10,050 retained entries never
    matters.
    """

    def __init__(self, maxlen: int = 10_000) -> None:
        self._maxlen = maxlen

    async def publish(
        self, channel: str, name: str, payload: dict[str, Any], *, version: int = 1
    ) -> None:
        """Append a message to the channel's stream."""
        try:
            await get_redis().xadd(
                build_key("stream", channel),
                {
                    "name": name,
                    "version": str(version),
                    "payload": json.dumps(payload),
                },
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:  # noqa: BLE001 - broadcast is best-effort
            logger.warning(
                "Stream publish failed",
                extra={"channel": channel, "message_name": name},
            )

    async def subscribe(
        self, *channels: str, last_id: str = "$"
    ) -> AsyncGenerator[tuple[str, Message]]:
        """Read messages from one or more streams, yielding the entry ID too.

        Args:
            *channels: Logical channel names.
            last_id: Where to start. ``"$"`` means "only what arrives from now",
                matching plain pub/sub. ``"0"`` replays everything retained.
                Pass the last ID you processed to resume exactly where you
                stopped — that resumption is the whole point of using Streams.

        Yields:
            ``(entry_id, message)`` pairs. **Persist the entry ID** as each
            message is handled; without it a restart cannot resume, and the
            durability advantage is lost.
        """
        client = get_redis()
        streams = {build_key("stream", channel): last_id for channel in channels}

        while True:
            # Blocking read: an idle subscriber costs nothing rather than
            # spinning through empty polls.
            # redis-py types the streams mapping more narrowly than it
            # accepts; a dict[str, str] is valid at runtime.
            response = await client.xread(  # ty: ignore[no-matching-overload]
                streams, block=5000, count=100
            )
            if not response:
                continue

            for stream_name, entries in response:
                channel = _strip_stream_prefix(stream_name)
                for entry_id, fields in entries:
                    entry = _text(entry_id)
                    # Advance the cursor before yielding, so a consumer that
                    # raises does not spin forever on the same entry.
                    streams[_text(stream_name)] = entry
                    message = _decode_stream_entry(channel, fields)
                    if message is not None:
                        yield entry, message


def _strip_stream_prefix(stream_name: Any) -> str:
    """Recover the logical channel name from a namespaced stream key."""
    return _text(stream_name).removeprefix(build_key("stream") + ":")


def _text(value: Any) -> str:
    """Decode a Redis value that may arrive as bytes."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode_stream_entry(channel: str, fields: dict[Any, Any]) -> Message | None:
    """Decode one stream entry, returning ``None`` when it is unusable."""
    decoded = {_text(key): _text(value) for key, value in fields.items()}
    try:
        return Message(
            channel=channel,
            name=decoded["name"],
            version=int(decoded.get("version", 1)),
            payload=json.loads(decoded.get("payload", "{}")),
        )
    except KeyError, ValueError, json.JSONDecodeError:
        logger.warning("Skipping undecodable stream entry", extra={"channel": channel})
        return None
