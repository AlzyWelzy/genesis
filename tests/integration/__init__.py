"""Integration tests — real external systems.

Exercise the infrastructure layer against a real PostgreSQL and Redis: session
behaviour, transaction boundaries, migration correctness, provider
implementations.

Slower than unit tests and dependent on running services; they belong in CI,
not in a save-triggered watch loop.
"""
