# 0009. Automatic feature-module discovery

**Status:** Accepted
**Date:** 2026-07-26

## Context

Four separate places needed to know which feature modules exist:

| Place | Needs | Failure when stale |
|---|---|---|
| `app/api.py` | routers | 404 — **obvious** |
| `migrations/env.py` | models | empty migration, table never created — **silent** |
| `app/core/lifespan.py` | event handlers | nothing subscribes — **silent** |
| `scripts/worker.py` | task handlers | jobs dead-lettered as unknown — **silent** |

Four hand-maintained import lists drift. Three of the four fail silently, and
those three fail in ways that pass every unit test.

## Decision

Discovery. `app/modules/registry.py` treats every direct subpackage of
`app.modules` as a feature and imports `router`, `models`, `handlers` and `tasks`
when present.

## Alternatives considered

**Keep the four lists.** Honest and explicit, and we started here. Rejected
because the silent failure modes are severe and the lists provide no value a
reader could not get from the directory listing.

**One central manifest.** Reduces four lists to one, but keeps the drift — the
manifest is still a thing to remember.

**Entry points / a plugin system.** Enormously more machinery for a fixed set of
first-party modules in one directory.

## Consequences

**Good.** Adding a feature requires no edit anywhere outside its own directory.
The silent failure modes are gone. Discovery order is sorted, so the OpenAPI
document is byte-for-byte reproducible.

**Bad.** Behaviour becomes implicit: a module is loaded because it exists, which
is less obvious than an import statement. Mitigated by keeping the convention
*narrow* — fixed submodule names, one fixed location, no manifest, no entry
points — so the whole mechanism is one readable file.

Import errors are deliberately **not** swallowed. A typo inside a model module
would otherwise be indistinguishable from that module not existing, and the
consequence is a table that is never created.

**Bad.** A module with a typo in its name is silently not a feature. Accepted:
that failure is visible immediately as missing routes.
