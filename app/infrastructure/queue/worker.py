"""Queue consumer.

Why this file exists
--------------------
:mod:`app.infrastructure.queue.client` is the producer side — what a service
calls. This is the consumer: the loop that reads jobs, dispatches them to
handlers, and decides what happens when one fails.

The retry policy is the substance here. Three behaviours that a naive worker
gets wrong, and each is a production incident:

**Retrying immediately.** A job failing because a third party is down will fail
again a millisecond later, and the retries themselves become the load that
keeps it down. Backoff is exponential.

**Retrying forever.** A job with a permanent bug — a malformed payload, a
deleted row — retries until someone notices, consuming a worker slot each time.
After ``max_retries`` it moves to the dead-letter queue.

**Losing in-flight work.** A worker killed mid-job must not silently drop it.
Consumer groups leave unacknowledged messages *pending*, and
:meth:`Worker.reclaim_stalled` claims those a dead worker left behind.

Run it with ``uv run python scripts/worker.py``.
"""

import asyncio
import json
from typing import Any, cast
from uuid import UUID

from redis.exceptions import ResponseError

from app.common.utils.datetime import utc_now
from app.core.context import request_scope
from app.core.logging import get_logger
from app.infrastructure.queue.client import (
    CONSUMER_GROUP,
    DEAD_LETTER_KEY,
    DELAYED_KEY,
    STREAM_KEY,
    DeliveredJob,
    TaskRegistry,
)
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)

#: Base for exponential backoff. Attempt N waits BASE * 2**N seconds.
_BACKOFF_BASE_SECONDS = 2

#: Cap, so a late retry is still same-day rather than next week.
_BACKOFF_MAX_SECONDS = 3600

#: A message pending longer than this is presumed abandoned by a dead worker.
_STALLED_AFTER_MS = 300_000

#: Shape of an ``XREADGROUP`` reply: one entry per stream, each holding a
#: list of ``(message_id, fields)`` pairs. redis-py types the whole reply as
#: ``Any``, so declaring it here documents the contract in one place instead
#: of forcing every consumer to re-derive it.
type StreamReply = list[tuple[Any, list[tuple[Any, dict[Any, Any]]]]]


class Worker:
    """Consumes jobs from the stream and dispatches them to handlers.

    Args:
        registry: Task registry mapping names to handlers.
        name: Consumer name, unique per process. Redis tracks pending messages
            per consumer, so two workers sharing a name cannot have each
            other's stalled jobs reclaimed correctly.
        concurrency: Jobs processed simultaneously by this worker.
    """

    def __init__(
        self,
        registry: TaskRegistry,
        *,
        name: str,
        concurrency: int = 4,
    ) -> None:
        self.registry = registry
        self.name = name
        self.concurrency = concurrency
        self._running = False
        self._semaphore = asyncio.Semaphore(concurrency)

    async def setup(self) -> None:
        """Create the consumer group if it does not exist.

        ``mkstream=True`` creates the stream too, so a fresh deployment does not
        need the first job to have been enqueued before a worker can start.
        """
        client = get_redis()
        try:
            await client.xgroup_create(
                build_key(STREAM_KEY), CONSUMER_GROUP, id="0", mkstream=True
            )
            logger.info("Created consumer group %s", CONSUMER_GROUP)
        except ResponseError as exc:
            # BUSYGROUP simply means another worker created it first.
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self) -> None:
        """Consume until stopped.

        Each pass promotes due delayed jobs, reclaims anything a dead worker
        abandoned, then reads a batch. Blocking reads mean an idle worker costs
        nothing rather than spinning.
        """
        await self.setup()
        self._running = True
        logger.info(
            "Worker started",
            extra={
                "worker": self.name,
                "concurrency": self.concurrency,
                "tasks": sorted(self.registry.names),
            },
        )

        while self._running:
            try:
                await self.promote_delayed()
                await self.reclaim_stalled()
                await self._consume_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker loop error; continuing")
                await asyncio.sleep(1)

    def stop(self) -> None:
        """Ask the loop to finish after the current batch.

        Graceful: in-flight jobs complete and are acknowledged, so a deploy does
        not strand work that then has to be reclaimed.
        """
        self._running = False

    async def promote_delayed(self) -> int:
        """Move due delayed jobs into the stream.

        Returns:
            How many jobs were promoted.
        """
        client = get_redis()
        key = build_key(DELAYED_KEY)
        now = utc_now().timestamp()

        due = await client.zrangebyscore(key, 0, now, start=0, num=100)
        promoted = 0
        for raw in due:
            # Remove first: if the process dies between the two operations, a
            # duplicate delivery is recoverable (handlers are idempotent) while
            # a lost job is not.
            entry = _text(raw)
            if await client.zrem(key, entry):
                await client.xadd(build_key(STREAM_KEY), json.loads(entry))
                promoted += 1
        return promoted

    async def reclaim_stalled(self) -> int:
        """Claim messages abandoned by a worker that died mid-job.

        Without this, a crashed worker's in-flight jobs stay pending forever —
        acknowledged by nobody and processed by nobody.

        Returns:
            How many messages were reclaimed.
        """
        client = get_redis()
        try:
            _, messages, _ = await client.xautoclaim(
                build_key(STREAM_KEY),
                CONSUMER_GROUP,
                self.name,
                min_idle_time=_STALLED_AFTER_MS,
                count=10,
            )
        except ResponseError:
            return 0

        for message_id, fields in messages:
            logger.warning(
                "Reclaimed stalled job", extra={"message_id": _text(message_id)}
            )
            await self._dispatch(_to_job(message_id, fields))
        return len(messages)

    async def _consume_batch(self) -> None:
        """Read and process one batch of new messages."""
        client = get_redis()
        response = cast(
            StreamReply,
            await client.xreadgroup(
                CONSUMER_GROUP,
                self.name,
                {build_key(STREAM_KEY): ">"},
                count=self.concurrency,
                block=5000,  # ms; an idle worker blocks rather than spinning
            ),
        )
        if not response:
            return

        jobs = [
            _to_job(message_id, fields)
            for _, entries in response
            for message_id, fields in entries
        ]
        await asyncio.gather(*(self._dispatch_guarded(job) for job in jobs))

    async def _dispatch_guarded(self, job: DeliveredJob) -> None:
        """Dispatch under the concurrency limit."""
        async with self._semaphore:
            await self._dispatch(job)

    async def _dispatch(self, job: DeliveredJob) -> None:
        """Run one job's handler and apply the retry policy on failure."""
        client = get_redis()
        try:
            handler = self.registry.resolve(job.name)
        except KeyError:
            # An unregistered task can never succeed, so retrying is pointless.
            logger.exception(
                "Unknown task; dead-lettering", extra={"job_name": job.name}
            )
            await self._dead_letter(job, reason="unknown_task")
            await client.xack(build_key(STREAM_KEY), CONSUMER_GROUP, job.message_id)
            return

        try:
            # Rebind the context the job was enqueued under. Two things need
            # it, and both fail silently or confusingly without it: log lines
            # would carry no correlation ID and be unlinkable to the request
            # that caused the work, and a handler touching a `TenantRepository`
            # would raise on `require_tenant_id()` — making tenant-scoped
            # background work impossible rather than merely untraceable.
            #
            # Bound per job, not per batch: jobs run concurrently under
            # `asyncio.gather`, and each task gets its own ContextVar copy, so
            # one job's tenant cannot leak into another's.
            async with request_scope(
                correlation_id=job.correlation_id,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
            ):
                await handler(job.payload)
        except Exception as exc:  # noqa: BLE001 - handler failures are expected
            await self._handle_failure(job, exc)
        else:
            logger.info(
                "Job completed",
                extra={"job_name": job.name, "job_id": job.job_id},
            )
        finally:
            # Acknowledged either way: a retry is re-enqueued as a *new*
            # message, so leaving this one pending would double-deliver it once
            # the stall timeout elapsed.
            await client.xack(build_key(STREAM_KEY), CONSUMER_GROUP, job.message_id)

    async def _handle_failure(self, job: DeliveredJob, exc: Exception) -> None:
        """Re-enqueue with backoff, or dead-letter once attempts are exhausted."""
        if not job.can_retry:
            logger.error(
                "Job exhausted its retries; dead-lettering",
                exc_info=exc,
                extra={"job_name": job.name, "job_id": job.job_id},
            )
            await self._dead_letter(job, reason=str(exc))
            return

        attempt = job.attempt + 1
        delay = min(_BACKOFF_BASE_SECONDS**attempt, _BACKOFF_MAX_SECONDS)
        logger.warning(
            "Job failed; retrying",
            exc_info=exc,
            extra={
                "job_name": job.name,
                "job_id": job.job_id,
                "attempt": attempt,
                "retry_in_seconds": delay,
            },
        )

        await get_redis().zadd(
            build_key(DELAYED_KEY),
            {
                json.dumps(
                    {
                        "id": job.job_id,
                        "name": job.name,
                        "payload": json.dumps(job.payload),
                        "attempt": str(attempt),
                        "max_retries": str(job.max_retries),
                        "enqueued_at": utc_now().isoformat(),
                        # Carried from the job, not from the ambient context:
                        # this runs in the worker's failure path, outside the
                        # scope the handler ran under. Dropping these here would
                        # mean attempt 1 has a tenant and attempt 2 does not —
                        # so a retried job silently loses the ability to do the
                        # tenant-scoped work that the first attempt could.
                        **_context_fields(job),
                    }
                ): (utc_now().timestamp() + delay)
            },
        )

    async def _dead_letter(self, job: DeliveredJob, *, reason: str) -> None:
        """Record a job that will not be retried.

        Kept rather than discarded: the payload is usually the only record of
        what was meant to happen, and it is what a replay needs after the bug
        is fixed. Alert when this list is non-empty.
        """
        await get_redis().rpush(
            build_key(DEAD_LETTER_KEY),
            json.dumps(
                {
                    "id": job.job_id,
                    "name": job.name,
                    "payload": job.payload,
                    "attempts": job.attempt + 1,
                    "reason": reason,
                    "failed_at": utc_now().isoformat(),
                }
            ),
        )


def _text(value: Any) -> str:
    """Decode a Redis value that may arrive as bytes."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _to_job(message_id: Any, fields: dict[Any, Any]) -> DeliveredJob:
    """Build a :class:`DeliveredJob` from a raw stream entry."""
    decoded = {_text(key): _text(value) for key, value in fields.items()}
    return DeliveredJob(
        message_id=_text(message_id),
        job_id=decoded.get("id", ""),
        name=decoded.get("name", ""),
        payload=json.loads(decoded.get("payload", "{}")),
        attempt=int(decoded.get("attempt", 0)),
        max_retries=int(decoded.get("max_retries", 3)),
        correlation_id=decoded.get("correlation_id"),
        tenant_id=_optional_uuid(decoded.get("tenant_id")),
        user_id=_optional_uuid(decoded.get("user_id")),
    )


def _context_fields(job: DeliveredJob) -> dict[str, str]:
    """Render a job's captured context back into flat stream fields.

    Absent values are omitted rather than written as ``"None"``, matching
    :meth:`~app.infrastructure.queue.client.Job.encode`, so a decoder cannot
    mistake the string for a value.
    """
    fields: dict[str, str] = {}
    if job.correlation_id is not None:
        fields["correlation_id"] = job.correlation_id
    if job.tenant_id is not None:
        fields["tenant_id"] = str(job.tenant_id)
    if job.user_id is not None:
        fields["user_id"] = str(job.user_id)
    return fields


def _optional_uuid(value: str | None) -> UUID | None:
    """Parse a UUID field that may be absent or unparseable.

    A malformed value is dropped rather than raised on: it would fail the job
    before the handler ever ran, and for context — which is metadata about the
    work, not the work itself — losing correlation is a far smaller harm than
    losing the job.
    """
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        logger.warning("Discarding an unparseable context UUID on a job")
        return None
