"""Tests that hold every test double to its real implementation's behaviour.

Why this directory exists
-------------------------
``conftest.py`` installs ``InMemoryCache``, ``InMemoryQueue`` and
``CollectingEmailProvider`` with *autouse* fixtures, so essentially every test in
this suite exercises the fake rather than the thing that will run in production.
That is the right design — a test reaching a real external system is a
monitoring check with a misleading name — and it has one failure mode that
nothing else can catch.

**If a fake is more permissive than the real implementation, the guarantee it
diverges on becomes untestable.** Not "untested": untestable. A test asserting
the real behaviour cannot pass, so whoever writes it deletes the assertion, and
the guarantee quietly stops being a guarantee.

That happened. ``InMemoryQueue`` ignored ``idempotency_key`` completely, so
enqueueing the same key three times produced three jobs against the fake and one
against Redis. Every unit test in the repository ran against the fake.

The general lesson, learned the same way twice now: **a test that does not
reproduce production structure verifies nothing.** Once for a route registered
with ``@app.get`` instead of ``include_router``; once for a queue that was not
really a queue.

How these are written
---------------------
Each test states one behaviour and asserts *both* implementations agree, running
the identical sequence against each. The assertion is on agreement, not on a
hard-coded expectation — a hard-coded expectation would have to be updated in two
places and would drift, which is the problem this exists to prevent.
"""
