# 0008. VARCHAR + CHECK instead of native PostgreSQL ENUM

**Status:** Accepted
**Date:** 2026-07-26

## Context

Enums are persisted constantly — statuses, tiers, kinds. PostgreSQL offers a
native `ENUM` type, and SQLAlchemy will use it by default.

## Decision

`VARCHAR` with a `CHECK` constraint, via
`app.common.enums.enum_column()` (`Enum(..., native_enum=False)`).

## Alternatives considered

**Native `ENUM`.** Compact and validated in the database. Rejected because
changing it is disproportionately painful:

- Adding a value requires `ALTER TYPE`, which historically could not run inside
  a transaction and still cannot be rolled back.
- Removing or reordering a value means creating a new type, rewriting every
  column that uses it, and dropping the old one — a long exclusive lock on a
  large table.

Enum values change far more often than anyone predicts when they choose the type.

**Plain `VARCHAR`, no constraint.** Cheapest to evolve, and loses database-side
validation entirely. A typo in application code then writes an invalid value
that nothing rejects until something reads it.

**A lookup table with a foreign key.** Correct for values users can define
(tags, categories). Overkill for a closed set the code branches on, and it adds
a join to every read.

## Consequences

**Good.** Adding a value is a cheap constraint swap. Invalid values are still
rejected by the database. The storage difference against native `ENUM` is
irrelevant at any realistic scale.

**Bad.** Slightly more storage than a native enum. The `CHECK` constraint must
be regenerated when the enum changes, which autogenerate handles because
`enum_column` names it deterministically.

**Note.** `enum_column` stores the enum's **value**, not its member name. The
value is the public contract — it appears in API responses and exports — while
the member name stays a renameable implementation detail. It also validates that
every member fits the column, so silent truncation is impossible.
