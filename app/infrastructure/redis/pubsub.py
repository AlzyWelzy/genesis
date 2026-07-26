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
Redis pub/sub is **fire and forget**: a subscriber that is down when a message
is published never receives it, and there is no replay. Never use it for
anything that must not be lost — use the queue, or Redis Streams.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol


class PubSub(Protocol):
    """Broadcast channel abstraction."""

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        """Send a message to every current subscriber of ``channel``."""
        ...

    def subscribe(self, *channels: str) -> AsyncIterator[dict[str, Any]]:
        """Yield messages from one or more channels until cancelled."""
        ...


class RedisPubSub:
    """Redis-backed :class:`PubSub` implementation.

    Messages are JSON objects so consumers in other languages can read them.
    """

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        """Serialise and publish a message."""
        raise NotImplementedError

    def subscribe(self, *channels: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe and yield decoded messages.

        The subscriber must own a dedicated connection: a connection in
        subscribe mode cannot serve normal commands, so borrowing one from the
        shared pool would deadlock unrelated callers.
        """
        raise NotImplementedError


# TODO: define the channel naming scheme (e.g. "<prefix>:<domain>:<event>") and
# a typed envelope (event name, version, payload, emitted_at) so consumers can
# reject messages they do not understand instead of crashing on a KeyError.
# TODO: decide whether Redis Streams replaces this where replay is required.
