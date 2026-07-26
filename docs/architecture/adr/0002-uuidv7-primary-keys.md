# 0002. UUIDv7 primary keys

**Status:** Accepted
**Date:** 2026-07-26

## Context

Primary key type must be decided before the first table. Changing it afterwards
means rewriting every table, every foreign key and every stored URL.

Requirements: identifiers appear in public URLs, must not leak business volume,
must be mergeable across environments, and must not degrade write performance as
tables grow.

## Decision

UUIDv7, generated in Python via `uuid.uuid7()` (stdlib, 3.14+), applied through
`UUIDPrimaryKeyMixin`.

## Alternatives considered

**bigint identity.** Fastest, smallest, best index locality. Rejected because
IDs appear in URLs: `/users/1` can be walked to `/users/2`, and the sequence
leaks both total row count and growth rate to anyone who registers twice a month.
Also collides when merging data across environments.

**UUIDv4.** Solves opacity, and is what most projects reach for. Rejected on
write performance: random keys scatter inserts across the entire B-tree, causing
constant page splits and a cache hit rate that degrades as the table grows —
precisely when it hurts most. The problem is invisible at 10k rows and severe at
10M.

**bigint PK with a separate public UUID.** Genuinely the best of both, and
rejected only on complexity: two columns and an extra unique index on every
table, and every repository must know which key to use in which context. That is
a per-query correctness burden forever, in exchange for an index-size win.

**ULID / KSUID.** Equivalent time-ordering benefits, but require a dependency and
a custom column type, and are not natively understood by PostgreSQL tooling.
UUIDv7 is the standardised form of the same idea.

## Consequences

Easy: exposing IDs publicly, minting IDs client-side, building object graphs and
publishing events before the `INSERT` without a round trip.

Hard: 16 bytes per key rather than 8, which shows up in wide indexes and large
join tables. Accepted.

Note: UUIDv7 embeds a creation timestamp. It is not a secret — anyone holding an
ID can read roughly when the row was created. Fine here; worth remembering
before using one as a security token, which it must never be.
