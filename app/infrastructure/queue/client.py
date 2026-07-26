"""Background job queue contract.

Why this file exists
--------------------
Anything slow, retryable or third-party — sending email, generating an export,
calling a payment provider's webhook back — must not run inside a request. A
request handler that awaits a five-second API call holds a worker, a database
connection and the client's socket for five seconds, and fails the whole
request if the third party blinks.

This module defines enqueueing as an explicit contract so services can hand off
work in one line and remain testable: in a test the queue collects jobs in a
list and nothing executes.

Design rules
------------
* **Jobs take serialisable arguments only.** Pass an entity's ID, never an ORM
  object — by the time the job runs, the session is closed and the row may have
  changed.
* **Jobs must be idempotent.** At-least-once delivery is the realistic
  guarantee; a retried job must not double-charge or double-send.
* **Enqueue after commit.** A job dispatched inside an open transaction can
  start before (or despite) the commit, and will not find the row it needs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of deferred work.

    Attributes:
        name: Registered task name the worker dispatches on. A string, not a
            function reference, so producers and consumers can be deployed and
            versioned independently.
        payload: JSON-serialisable arguments.
        max_retries: Attempts before the job is moved to the dead-letter queue.
        delay: Wait before the job becomes eligible to run.
        idempotency_key: When set, enqueueing the same key twice is a no-op.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 3
    delay: timedelta | None = None
    idempotency_key: str | None = None


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


#: A task handler: receives the decoded payload, returns nothing useful.
#: Raising signals failure and triggers the retry policy.
type TaskHandler = Callable[[dict[str, Any]], Awaitable[None]]


class TaskRegistry:
    """Maps task names to handlers.

    A registry rather than direct imports so the worker can validate at startup
    that every name it might receive has a handler, instead of discovering a
    missing one when the message arrives.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, name: str) -> Callable[[TaskHandler], TaskHandler]:
        """Decorator registering a handler under ``name``.

        Raises:
            ValueError: When the name is already registered — a silent
                overwrite would route jobs to the wrong code.
        """
        raise NotImplementedError

    def resolve(self, name: str) -> TaskHandler:
        """Look up a handler.

        Raises:
            KeyError: When no handler is registered for the name.
        """
        raise NotImplementedError


# TODO: pick a backend (arq / Dramatiq / Celery / Redis Streams) and implement
# a concrete Queue plus a worker entry point under scripts/.
# TODO: define the dead-letter policy and an alert when it is non-empty.
# TODO: add the transactional outbox pattern if a job must never be lost when
# the enqueue succeeds but the transaction later rolls back.
