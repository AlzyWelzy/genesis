# 0004. snake_case, enveloped, URL-versioned API

**Status:** Accepted
**Date:** 2026-07-26

## Context

Naming, response shape and versioning are trivial to change before any client
exists and expensive afterwards — each one becomes a breaking change requiring a
coordinated release with every consumer.

## Decision

- **`snake_case`** for all field names, query parameters and error codes.
- **Enveloped responses**: `{"data": ...}` for single resources,
  `{"items": [...], "meta": {...}}` for collections, `{"error": {...}}` for
  failures.
- **URL versioning**: `/api/v1/...`, applied once in `app/api.py`.

## Alternatives considered

**camelCase fields.** Friendlier to JavaScript consumers, and a Pydantic
`alias_generator` makes it mechanical. Rejected because the mapping layer is not
free: it must be configured on every schema with `populate_by_name`, and it makes
the field name in a log line, a database column and an API response three
different strings — which is a real cost when debugging from a customer report.
A JS client can map at its own boundary, which is one place rather than several
hundred.

**Bare responses (no envelope).** Leanest payloads, and arguably more RESTful.
Rejected because a bare array cannot grow: adding pagination metadata,
deprecation notices or rate-limit state later is a breaking change for every
client. Envelopes cost four characters and preserve that option. The common
workaround — pagination in headers — is worse, since headers are awkward to read
in most client libraries and invisible in a browser's network tab preview.

**Header-based versioning (`Accept: application/vnd.genesis.v2+json`).**
Purer, and keeps URLs stable across versions. Rejected on operability: a version
in the URL is visible in access logs, in a browser address bar, in a support
ticket and in a proxy routing rule. Header negotiation makes "which version is
this customer on?" a question requiring instrumentation.

**No versioning; evolve additively forever.** Rejected as wishful. It works until
the first genuine breaking change, at which point there is no mechanism.

## Consequences

Easy: adding top-level response metadata, routing by version at the proxy,
reading logs and reproducing requests by hand.

Hard: JavaScript clients see `snake_case` unless they map. Accepted.

Commits us to: `error.code` values being permanent. Adding a code is safe;
changing one is breaking. And to versioning staying a *routing* concern — if a
service ever needs to know which version called it, the boundary has been drawn
in the wrong place.
