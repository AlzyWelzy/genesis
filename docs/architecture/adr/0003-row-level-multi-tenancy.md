# 0003. Row-level multi-tenancy

**Status:** Accepted
**Date:** 2026-07-26

## Context

The platform is multi-tenant. The isolation model determines the shape of every
model, every query and every migration, and cannot be changed later without a
full data migration.

## Decision

Row-level isolation: every tenant-scoped table carries `tenant_id`, applied via
`TenantMixin`.

The filter is enforced **structurally**, not by convention:
`TenantRepository.select()` applies the tenant predicate before the caller sees
the statement, and `add()` stamps the tenant on insert. The tenant comes from
`app.core.context`, populated once at the edge by an authenticated dependency —
never from a method argument or a request body.

## Alternatives considered

**Schema per tenant.** Strong isolation, easy per-tenant backup and restore, and
a hard boundary that no application bug can cross. Rejected on operations: every
migration must run against every schema (an hour at a thousand tenants, with
partial-failure states), connection pooling churns on `search_path` switches, and
PostgreSQL's catalog degrades in the thousands of schemas. The right choice for
tens of large enterprise tenants; wrong for many small ones.

**Database per tenant.** Strongest isolation, and the only model that satisfies
some data-residency requirements. Rejected: connection pool per tenant does not
scale, and provisioning becomes a distributed systems problem.

**PostgreSQL row-level security (RLS).** Genuinely appealing — the database
enforces isolation, so an application bug cannot leak data. Rejected for now
because it requires a session variable set per checked-out connection, which
interacts badly with pooling; policies are invisible from the application and
surprising to debug; and the superuser and table-owner bypasses need care. Worth
revisiting as **defence in depth** on top of the repository filter, not instead
of it.

**Application-level filtering by convention.** Rejected outright. A missed
`WHERE tenant_id = ?` returns another customer's data with a 200 status and
nothing in the response, the logs or the tests to indicate it. Conventions are
enforced by attention, and there will be thousands of queries.

## Consequences

Easy: one schema, one migration run, cheap tenant creation, cross-tenant
analytics when deliberately needed.

Hard: the filter must never be missed. Mitigated by the repository base, and it
must be backed by a scoping test per tenant-scoped repository — see
[`testing-strategy.md`](../testing-strategy.md).

Every composite index must lead with `tenant_id`, since PostgreSQL can only use
a leading prefix and effectively every query starts there.

`require_tenant_id()` raises rather than returning `None`, so a missing tenant is
a 500 and a stack trace instead of a silent cross-tenant read. That is the
correct trade.
