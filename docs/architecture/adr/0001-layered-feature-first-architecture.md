# 0001. Layered, feature-first architecture

**Status:** Accepted
**Date:** 2026-07-26

## Context

The platform is expected to reach hundreds of endpoints and be worked on by many
developers concurrently. Two structural choices had to be made before the first
feature: how to group code, and how to constrain dependencies between groups.

## Decision

Four layers — `core`, `infrastructure`, `common`, `modules` — with dependencies
pointing inward. Nothing in `core`, `common` or `infrastructure` may import from
`modules`.

Within `modules`, code is grouped **by feature**, not by technical role. Each
feature owns its router, service, repository, models and schemas in one
directory.

Within a feature: router → dependency → service → repository → infrastructure.
The service owns business rules and the transaction boundary; the repository
owns SQL and commits nothing.

## Alternatives considered

**Layer-first (`models/`, `services/`, `routers/`).** Rejected: at scale a single
feature change touches five distant directories, unrelated features collide in
the same files, and the tree says nothing about what the product does. It is
genuinely fine below ~20 files, which is why it is such a common early choice.

**Full hexagonal architecture with ports, adapters and domain entities separate
from ORM models.** Rejected as disproportionate. The main benefit — swapping the
persistence layer — is not a real requirement, and the cost is a mapping layer
between domain objects and ORM models on every read and write, which every
developer pays for on every feature.

**No repository layer; services query directly.** Rejected: it is the shorter
path, but it puts SQL and business rules in one file, makes tenant scoping
impossible to enforce structurally, and leaves query optimisation scattered.

**Flat structure, no enforced layers.** Rejected: works until roughly the third
developer, then decays without anyone deciding it should.

## Consequences

Easy: finding a feature's code, extracting a feature into its own service,
testing services without HTTP or a database, changing an infrastructure provider.

Hard: sharing logic between features — deliberately, since the alternative is
implicit coupling. Cross-feature work goes through a service call, a shared
module, or an event.

Commits us to: the discipline being enforced in review and CI. One inward import
makes the next easier to justify.
