# 0007. Transactional outbox for durable events

**Status:** Accepted
**Date:** 2026-07-26

## Context

There is a gap between committing a transaction and publishing what happened:

```python
await repo.add(invoice)
await session.commit()  # (1)
await queue.enqueue(receipt)  # (2)
```

A crash between (1) and (2) loses the receipt permanently. The invoice exists,
nothing will ever send the email, and no error was raised anywhere.

Reordering does not help. Publishing before the commit means a worker can pick
the job up and find no invoice, or the transaction rolls back and a receipt is
sent for something that never happened.

The gap cannot be closed by care: Redis and PostgreSQL are separate systems and
there is no transaction spanning both.

## Decision

An outbox table written **inside** the business transaction, plus a relay that
publishes staged rows. `app/infrastructure/outbox/`.

Opt-in per call site — `publish_after_commit(..., durable=True)` — rather than
mandatory for every event.

## Alternatives considered

**Two-phase commit.** Neither Redis nor our queue supports it, and XA
transactions are operationally miserable where they do exist.

**Publish-then-commit.** Trades silent loss for phantom events, which is worse:
a receipt for a cancelled invoice is visible to the customer.

**Accept the loss.** Reasonable for cache invalidation, where the next write
republishes anyway. Unacceptable for payments and provisioning. Hence opt-in
rather than all-or-nothing.

**Change data capture (Debezium et al).** Genuinely durable and requires no
application change, but adds Kafka plus a CDC pipeline to run — enormous
operational weight for a service of this size.

## Consequences

**Good.** No event is lost once its transaction commits. The outbox row carries
the tenant and correlation ID, so a queued job's logs stay attached to the
request that caused it. `pending_count()` is a single number that reveals a
stalled relay.

**Bad.** One extra insert per durable event. A relay process must run, and a
stopped relay silently accumulates unpublished work — which is why the pending
count must be alerted on. Delivery is **at least once**, so every handler must
be idempotent. Ordering is not guaranteed across concurrent relays; anything
needing strict per-entity ordering must carry a sequence number.

**Revisit if** the outbox table becomes a write hotspot, at which point
partitioning by tenant or moving to CDC becomes worth the operational cost.
