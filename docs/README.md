# Documentation

| Document | Contents |
| --- | --- |
| [`architecture.md`](architecture.md) | The four layers, the request flow, transaction boundaries, and why the structure is what it is |

## Where documentation lives

Documentation belongs as close to the code as possible, because that is the only
version that gets updated:

- **Why a file exists** → its module docstring.
- **How a feature is structured** → [`app/modules/README.md`](../app/modules/README.md).
- **How to test** → [`tests/README.md`](../tests/README.md).
- **Migration rules** → [`migrations/README.md`](../migrations/README.md).
- **Setup and commands** → the root [`README.md`](../README.md).

This directory is for what does not fit in a docstring: cross-cutting design,
decisions and their rationale.

## Suggested additions

- `decisions/` — one short ADR per significant choice (multi-tenancy model,
  queue backend, primary key type). Record the alternatives and *why they were
  rejected*; that is the part nobody can reconstruct a year later.
- `runbooks/` — what to do when the queue backs up, the database fails over, or
  a migration stalls mid-deploy.
- `onboarding.md` — the first-day path for a new developer.

## Rule

A pull request that changes behaviour updates the documentation that describes
it, in the same commit. Documentation corrected in a follow-up is documentation
that stays wrong for a week.
