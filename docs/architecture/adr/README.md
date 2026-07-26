# Architecture decision records

One short document per significant decision. The purpose is not to describe
what the code does — the code does that — but to record **what was rejected and
why**, which is the part nobody can reconstruct a year later.

Write one when a decision is expensive to reverse, when it constrains future
work, or when a reasonable person would ask "why did they do it that way?".

Do not write one for a decision that a reader can infer from the code, or for
something a single comment covers.

## Format

Number sequentially: `0001-short-title.md`.

```markdown
# NNNN. Title

**Status:** Accepted | Superseded by ADR-NNNN
**Date:** YYYY-MM-DD

## Context
The situation and the constraints. What made a decision necessary.

## Decision
What was decided, in the active voice.

## Alternatives considered
Each option, with the concrete reason it was rejected. This section is the
point of the document.

## Consequences
What this makes easy, what it makes hard, and what it commits us to.
```

**Never edit an accepted ADR.** Supersede it with a new one and update the
status of the old. The record of what was believed and when is the value.

## Index

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-layered-feature-first-architecture.md) | Layered, feature-first architecture | Accepted |
| [0002](0002-uuidv7-primary-keys.md) | UUIDv7 primary keys | Accepted |
| [0003](0003-row-level-multi-tenancy.md) | Row-level multi-tenancy | Accepted |
| [0004](0004-api-conventions.md) | snake_case, enveloped, URL-versioned API | Accepted |
| [0005](0005-ed25519-token-signing.md) | Ed25519 token signing with rotation | Accepted |

## Still to record

- Queue backend (arq / Dramatiq / Celery / Redis Streams)
- Authorization model (RBAC / ABAC / scopes)
- Soft deletion policy
- Search strategy
