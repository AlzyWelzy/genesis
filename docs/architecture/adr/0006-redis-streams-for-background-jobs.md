# 0006. Redis Streams for background jobs

**Status:** Accepted
**Date:** 2026-07-26

## Context

Anything slow, retryable or third-party must run outside the request cycle. A
handler that awaits a five-second API call holds a worker, a database connection
and the client's socket for five seconds, and fails the request if the third
party blinks.

We needed a job queue with at-least-once delivery, retries with backoff, delayed
execution and a dead-letter path.

## Decision

Redis Streams, with consumer groups. Implemented in
`app/infrastructure/queue/`.

## Alternatives considered

**Celery.** The default answer, and rejected for three reasons: a large
dependency tree, a synchronous worker model that fits badly with an async
codebase, and a configuration surface far wider than we need.

**arq / Dramatiq.** Both async-native and both good. Rejected because they add a
dependency for capability we already have — Redis is already required for cache,
pub/sub and rate limiting, so Streams add no new infrastructure to run, monitor
or pay for.

**A plain Redis list (`LPUSH`/`BRPOP`).** Simplest, and unacceptable: there is no
acknowledgement. A worker that crashes mid-job loses the message silently. That
single property rules it out for anything that matters.

**PostgreSQL as a queue (`SKIP LOCKED`).** Genuinely viable, and we use exactly
this pattern for the outbox. Rejected for general job processing because job
throughput would compete with application queries for the same connection pool.

## Consequences

**Good.** No new infrastructure. Consumer groups give acknowledgement, so a
crashed worker's in-flight jobs are reclaimable rather than lost. The producer
side is a protocol, so the backend is replaceable without touching callers.

**Bad.** We implement the retry policy, backoff, dead-lettering and stalled-job
reclamation ourselves — roughly 300 lines that Celery would have provided. Redis
persistence is weaker than PostgreSQL's, so a Redis data loss loses queued jobs;
anything that must survive that belongs in the outbox instead.

**Revisit if** job volume outgrows a single Redis instance, or scheduling needs
(cron-like recurrence, workflows, chaining) grow beyond what a simple delayed
set expresses.
