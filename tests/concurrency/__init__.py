"""Tests that run operations against each other, not one at a time.

Why this directory exists
-------------------------
The property suite closed the input-dependent bug class: invariants over pure
functions now hold for generated input rather than for inputs somebody thought
of. It cannot reach a different class at all, because the failure needs *two
things happening at once*.

Every guarantee this platform makes has a concurrent clause, and each one is a
real production failure when it breaks:

* ``FOR UPDATE SKIP LOCKED`` means two relays share the outbox rather than
  double-publishing it — so a customer is not charged twice during a deploy,
  which is exactly when two relays are running.
* A consumer group means two workers split a stream rather than both running the
  same job.
* ``SET NX`` means two producers racing one idempotency key produce one job.
* An optimistic-lock version means the second of two concurrent editors is told
  to refetch instead of silently overwriting the first.
* A rate limit shared across replicas means N requests are allowed, not N per
  replica.

Every one of those is invisible to a sequential test. Run the operations one
after another and the code passes whether or not the locking is correct — which
is the same structural blind spot the property suite fixed for inputs, in a
dimension property tests cannot express.

How these are written
---------------------
Real PostgreSQL and real Redis, always. The guarantees being tested *are*
PostgreSQL's and Redis's; a fake would assert only that we called the methods we
believed we were calling.

Concurrency is created with ``asyncio.gather`` over genuinely separate sessions
or clients. Sharing one session between "concurrent" operations tests nothing:
a single session serialises them, and the test passes on code that would
deadlock or double-write in production.

Where a race needs a specific interleaving rather than merely simultaneous
starts, use an ``asyncio.Event`` to hold one side at the exact point the other
must overtake it. A ``sleep`` in place of that is a coin flip that passes on a
fast machine and fails in CI.
"""
