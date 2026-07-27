"""Property-based tests.

Why this directory exists
-------------------------
Every other test in this suite checks that a function behaves correctly for
inputs *somebody thought of*. That is a structural limit, not a diligence
problem, and it is the one that kept letting bugs through:

* the pagination cursor was rejected for ~7% of inputs, and every hand-written
  cursor test passed because each happened to land in the other 93%;
* ``deep_merge`` returned a result aliasing its own inputs, and the test named
  ``does_not_mutate_inputs`` passed because it asserted the other half of the
  property;
* ``to_payload`` could not serialise a ``date``, and no test carried one;
* ``timedelta`` lost microseconds past a few hundred years, and no test used a
  duration that long.

None of those is obscure. They are invisible to example-based tests for one
reason: the author picks the input, and an author who had thought of the failing
input would have fixed the bug instead of writing a test that passes.

Property tests invert that. They state an invariant that must hold for *every*
input and let Hypothesis hunt for a counterexample — the empty string, the lone
surrogate, the duration of a hundred thousand days, the value differing only by
Unicode normalisation form. When one fails, it shrinks the input to the smallest
case that still breaks, which is usually enough to see the cause immediately.

What belongs here
-----------------
Invariants over pure functions: round-trips, bounds, idempotence, ordering,
and independence between a result and its inputs. Anything needing PostgreSQL or
Redis belongs in ``tests/integration/`` — Hypothesis runs a test hundreds of
times, and stateful I/O makes that both slow and unreliable.

How to state a property
-----------------------
Prefer a relationship that must hold universally over a restatement of the
implementation. ``len(truncate(s, n)) <= n`` is a property. ``truncate("abcdef",
3) == "ab…"`` is an example, and belongs beside its siblings in the example
suite.

A property that fails is not automatically a bug in the code — it is a
disagreement between the code and a claim about it, and sometimes the claim is
wrong. Several properties here were rewritten because the first version asserted
something the function never promised. Read the shrunk counterexample before
changing either side.
"""
