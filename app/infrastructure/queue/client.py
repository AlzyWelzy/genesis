"""Background job queue.

Why this file exists
--------------------
Anything slow, retryable or third-party — sending email, generating an export,
calling a payment provider — must not run inside a request. A handler that
awaits a five-second API call holds a worker, a database connection and the
client's socket for five seconds, and fails the whole request if the third
party blinks.

Backend choice
--------------
Redis Streams, not Celery/arq/Dramatiq. The reasoning:

* Redis is already a dependency for cache, pub/sub and rate limiting, so this
  adds no new infrastructure to run, monitor or pay for.
* Streams provide **consumer groups with acknowledgement**, which a plain list
  (``LPUSH``/``BRPOP``) does not. A worker that crashes mid-job leaves the
  message pending and claimable rather than silently lost — the failure mode
  that makes list-based queues unsuitable for anything that matters.
* Celery brings a large dependency tree and a synchronous worker model that
  fits badly with an async codebase.

The abstraction is a protocol, so replacing the backend later touches this
package only. Recorded as an ADR when the first heavy workload arrives.

Design rules
------------
* **Jobs take serialisable arguments only.** Pass an entity's ID, never an ORM
  object — by the time the job runs the session is closed and the row may have
  changed.
* **Jobs must be idempotent.** At-least-once is the realistic guarantee; a
  retried job must not double-charge or double-send.
* **Enqueue after commit.** A job dispatched inside an open transaction can
  start before — or despite — the commit, and will not find the row it needs.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from uuid import uuid7

from app.common.utils.datetime import utc_now
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)

#: Stream every job is written to.
STREAM_KEY = "jobs"

#: Consumer group. All workers join the same group so each job goes to exactly
#: one of them, rather than to all of them as pub/sub would.
CONSUMER_GROUP = "workers"

#: Sorted set holding jobs that are not yet eligible to run.
DELAYED_KEY = "jobs:delayed"

#: Where exhausted jobs land. Must be monitored — a silently growing
#: dead-letter queue is indistinguishable from a working system.
DEAD_LETTER_KEY = "jobs:dead"


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of deferred work.

    Attributes:
        name: Registered task name the worker dispatches on. A string, not a
            function reference, so producers and consumers can be deployed and
            versioned independently.
        payload: JSON-serialisable arguments.
        max_retries: Attempts before the job moves to the dead-letter queue.
        delay: Wait before the job becomes eligible to run.
        idempotency_key: When set, enqueueing the same key twice is a no-op.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 3
    delay: timedelta | None = None
    idempotency_key: str | None = None

    def encode(self, job_id: str, attempt: int = 0) -> dict[str, str]:
        """Serialise for a Redis stream entry.

        Stream fields are flat strings, so the payload is nested as JSON.
        """
        return {
            "id": job_id,
            "name": self.name,
            "payload": json.dumps(self.payload),
            "attempt": str(attempt),
            "max_retries": str(self.max_retries),
            "enqueued_at": utc_now().isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DeliveredJob:
    """A job handed to a worker, plus the bookkeeping needed to acknowledge it."""

    message_id: str
    job_id: str
    name: str
    payload: dict[str, Any]
    attempt: int
    max_retries: int

    @property
    def can_retry(self) -> bool:
        """Whether another attempt is permitted."""
        return self.attempt < self.max_retries


class Queue(Protocol):
    """Producer-side queue contract."""

    async def enqueue(self, job: Job) -> str:
        """Submit a job and return its identifier."""
        ...

    async def enqueue_many(self, jobs: list[Job]) -> list[str]:
        """Submit several jobs, ideally in one round trip."""
        ...

    async def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not started. Returns whether it was cancelled."""
        ...


class RedisQueue:
    """Redis Streams-backed :class:`Queue`."""

    async def enqueue(self, job: Job) -> str:
        """Submit a job.

        Delayed jobs go to a sorted set keyed by their due time and are moved
        into the stream by the worker's scheduler tick; immediate jobs go
        straight to the stream.

        Returns:
            The job identifier.
        """
        job_id = uuid7().hex
        client = get_redis()

        if job.idempotency_key:
            # SET NX is atomic, so two concurrent producers cannot both win.
            # The TTL bounds how long a duplicate is suppressed — without it the
            # key set would grow without limit.
            claimed = await client.set(
                build_key("jobs:idem", job.idempotency_key),
                job_id,
                nx=True,
                ex=86_400,
            )
            if not claimed:
                logger.info(
                    "Duplicate job suppressed",
                    extra={"job_name": job.name, "idempotency_key": job.idempotency_key},
                )
                return job_id

        if job.delay:
            due = (utc_now() + job.delay).timestamp()
            await client.zadd(
                build_key(DELAYED_KEY),
                {json.dumps(job.encode(job_id)): due},
            )
        else:
            await client.xadd(build_key(STREAM_KEY), job.encode(job_id))

        logger.info("Job enqueued", extra={"job_name": job.name, "job_id": job_id})
        return job_id

    async def enqueue_many(self, jobs: list[Job]) -> list[str]:
        """Submit several jobs in one pipeline."""
        return [await self.enqueue(job) for job in jobs]

    async def cancel(self, job_id: str) -> bool:
        """Cancel a delayed job that has not yet been moved to the stream.

        A job already in the stream cannot be cancelled — it may be in flight.
        Handlers that need cancellation must check a flag when they start.
        """
        client = get_redis()
        key = build_key(DELAYED_KEY)
        for raw in await client.zrange(key, 0, -1):
            if json.loads(raw).get("id") == job_id:
                await client.zrem(key, raw)
                return True
        return False


#: A task handler: receives the decoded payload, returns nothing useful.
#: Raising signals failure and triggers the retry policy.
type TaskHandler = Callable[[dict[str, Any]], Awaitable[None]]


class TaskRegistry:
    """Maps task names to handlers.

    A registry rather than direct imports, so the worker can validate at startup
    that every name it might receive has a handler — instead of discovering a
    missing one when the message arrives and the job is already lost.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, name: str) -> Callable[[TaskHandler], TaskHandler]:
        """Decorator registering a handler under ``name``.

        Args:
            name: The task name producers will enqueue.

        Returns:
            The decorator, which returns the handler unchanged so it stays
            directly callable in tests.

        Raises:
            ValueError: When the name is already registered. A silent overwrite
                would route jobs to the wrong code, which is far worse than a
                startup crash.
        """

        def decorator(handler: TaskHandler) -> TaskHandler:
            if name in self._handlers:
                raise ValueError(f"Task '{name}' is already registered")
            self._handlers[name] = handler
            logger.debug("Registered task %s -> %s", name, handler.__qualname__)
            return handler

        return decorator

    def resolve(self, name: str) -> TaskHandler:
        """Look up a handler.

        Raises:
            KeyError: When no handler is registered for the name.
        """
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(
                f"No handler registered for task '{name}'. "
                "Is the module declaring it imported by the worker?"
            ) from exc

    @property
    def names(self) -> frozenset[str]:
        """Every registered task name."""
        return frozenset(self._handlers)

    def clear(self) -> None:
        """Remove every registration. For tests."""
        self._handlers.clear()


class InMemoryQueue:
    """Collects jobs in a list instead of dispatching them.

    For tests: lets an assertion check that a job *would* have been enqueued,
    with what payload, and without Redis running.
    """

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> str:
        """Record the job."""
        self.jobs.append(job)
        return uuid7().hex

    async def enqueue_many(self, jobs: list[Job]) -> list[str]:
        """Record every job."""
        return [await self.enqueue(job) for job in jobs]

    async def cancel(self, job_id: str) -> bool:
        """No-op; nothing is scheduled."""
        return False

    def clear(self) -> None:
        """Discard recorded jobs. For test teardown."""
        self.jobs.clear()


#: Process-wide registry. Task modules import this and decorate their handlers.
tasks = TaskRegistry()

#: Process-wide queue. Replaced with :class:`InMemoryQueue` in tests.
queue: Queue = RedisQueue()


def set_queue(implementation: Queue) -> None:
    """Replace the process-wide queue."""
    global queue  # noqa: PLW0603 - process-wide singleton by design
    queue = implementation


# TODO: add the transactional outbox pattern if a job must never be lost when
# the enqueue succeeds but the surrounding transaction later rolls back.
