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
from uuid import UUID, uuid7

from app.common.utils.datetime import utc_now
from app.core.context import current_context
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)


def _as_text(value: object) -> str:
    """Coerce a Redis response value to text.

    redis-py types its responses as ``Any``, so values arrive as bytes
    or str depending on the client's decode setting. Normalising here
    keeps every caller from re-deciding.
    """
    return value.decode() if isinstance(value, bytes) else str(value)


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

        The ambient request context is captured here, at enqueue time, because
        this is the last moment it exists — the worker runs in a different
        process with no notion of the request that caused the job. Two things
        depend on it:

        * **Correlation.** Without it, a job's log lines are orphans and no
          support query can connect them to the user action that produced them.
        * **Tenancy.** A handler using a ``TenantRepository`` reads the tenant
          from the ambient context, so a job enqueued without one cannot do
          tenant-scoped work at all — ``require_tenant_id()`` raises.

        Omitted rather than written as ``"None"`` when absent, so a decoder
        cannot mistake the string for a value.
        """
        context = current_context()
        fields = {
            "id": job_id,
            "name": self.name,
            "payload": json.dumps(self.payload),
            "attempt": str(attempt),
            "max_retries": str(self.max_retries),
            "enqueued_at": utc_now().isoformat(),
        }
        if context.correlation_id is not None:
            fields["correlation_id"] = context.correlation_id
        if context.tenant_id is not None:
            fields["tenant_id"] = str(context.tenant_id)
        if context.user_id is not None:
            fields["user_id"] = str(context.user_id)
        return fields


@dataclass(frozen=True, slots=True)
class DeliveredJob:
    """A job handed to a worker, plus the bookkeeping needed to acknowledge it."""

    message_id: str
    job_id: str
    name: str
    payload: dict[str, Any]
    attempt: int
    max_retries: int

    #: Context captured by :meth:`Job.encode` in the process that enqueued the
    #: job. ``tenant_id`` is load-bearing rather than decorative: a handler
    #: using a ``TenantRepository`` reads the tenant from the ambient context,
    #: so a job dispatched without it cannot do tenant-scoped work at all.
    correlation_id: str | None = None
    tenant_id: UUID | None = None
    user_id: UUID | None = None

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
                    extra={
                        "job_name": job.name,
                        "idempotency_key": job.idempotency_key,
                    },
                )
                # The *winner's* ID, not the one just minted. Returning the
                # fresh ID hands back an identifier for a job that was never
                # enqueued: cancelling it silently does nothing, and storing it
                # against a business record points at nothing that exists.
                # The claim's value is the winner's ID precisely so this is
                # answerable.
                winner = await client.get(build_key("jobs:idem", job.idempotency_key))
                return _as_text(winner) if winner else job_id

        if job.delay:
            due = (utc_now() + job.delay).timestamp()
            await client.zadd(
                build_key(DELAYED_KEY),
                {json.dumps(job.encode(job_id)): due},
            )
        else:
            # redis-py's stubs type stream fields more narrowly than the
            # client actually accepts; a dict[str, str] is valid at runtime.
            await client.xadd(  # ty: ignore[no-matching-overload]
                build_key(STREAM_KEY), job.encode(job_id)
            )

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
            entry = _as_text(raw)
            if json.loads(entry).get("id") == job_id:
                await client.zrem(key, entry)
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
            logger.debug(
                "Registered task %s -> %s",
                name,
                getattr(handler, "__qualname__", repr(handler)),
            )
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

    **It must behave like :class:`RedisQueue` wherever behaviour is observable.**
    This fake is installed by an autouse fixture, so it is what essentially every
    test in the suite actually exercises. A fake that is more permissive than the
    real implementation does not merely fail to catch a bug — it makes the
    guarantee untestable, because the test that would prove it cannot pass.

    That happened here: idempotency was not implemented at all, so enqueueing the
    same key three times produced three jobs against the fake and one against
    Redis. Any code depending on that guarantee would have been "verified" by
    tests that could not observe it. See ``tests/parity``.
    """

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        #: Idempotency key to the ID of the job that claimed it, mirroring the
        #: ``SET NX`` in :meth:`RedisQueue.enqueue`.
        self._claimed: dict[str, str] = {}

    async def enqueue(self, job: Job) -> str:
        """Record the job, honouring ``idempotency_key`` as Redis does.

        Returns the *winning* job's ID when a duplicate is suppressed, so the
        caller holds an identifier for a job that actually exists.
        """
        if job.idempotency_key:
            if claimed := self._claimed.get(job.idempotency_key):
                logger.info(
                    "Duplicate job suppressed",
                    extra={
                        "job_name": job.name,
                        "idempotency_key": job.idempotency_key,
                    },
                )
                return claimed
            job_id = uuid7().hex
            self._claimed[job.idempotency_key] = job_id
            self.jobs.append(job)
            return job_id

        self.jobs.append(job)
        return uuid7().hex

    async def enqueue_many(self, jobs: list[Job]) -> list[str]:
        """Record every job."""
        return [await self.enqueue(job) for job in jobs]

    async def cancel(self, job_id: str) -> bool:  # noqa: ARG002 - protocol shape
        """No-op; nothing is ever scheduled, so nothing can be cancelled."""
        return False

    def clear(self) -> None:
        """Discard recorded jobs and idempotency claims. For test teardown."""
        self.jobs.clear()
        self._claimed.clear()


#: Process-wide registry. Task modules import this and decorate their handlers.
tasks = TaskRegistry()

#: Process-wide queue. Replaced with :class:`InMemoryQueue` in tests.
queue: Queue = RedisQueue()


def set_queue(implementation: Queue) -> None:
    """Replace the process-wide queue."""
    global queue  # noqa: PLW0603 - process-wide singleton by design
    queue = implementation


def get_queue() -> Queue:
    """Return the current process-wide queue.

    Always reach the queue through this, never ``from ... import queue``. A
    ``from``-import binds the object into the importing module's namespace at
    import time, so a later :func:`set_queue` rebinds this module's name and
    leaves every such importer still holding the old one. In tests that means
    the in-memory fake is installed and the real Redis queue is used anyway —
    which fails as a connection error at best, and silently enqueues into a
    developer's live Redis at worst.

    Matches ``get_redis``, ``get_storage``, ``get_email_provider`` and
    ``get_metrics``, which exist for the same reason.
    """
    return queue


# For a job that must never be lost when the enqueue succeeds but the
# surrounding transaction later rolls back, stage a domain event through
# app.infrastructure.outbox instead of enqueueing directly. The outbox row
# commits atomically with the business change, and its relay does the
# enqueueing afterwards — see docs/architecture/adr/0007-transactional-outbox.md.
