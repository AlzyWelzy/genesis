# API guidelines

These decisions are cheap now and expensive once clients depend on them. Follow
them exactly; deviating "just here" is how an API becomes a collection of
special cases.

## Naming

**`snake_case` everywhere** — fields, query parameters, error codes.

Chosen over `camelCase` because it needs no alias layer: the field name is the
same in the model, the log line, the database column and the response. A
JavaScript client can map at its boundary, which is one place, whereas an alias
generator on every schema is several hundred.

Resources are **plural nouns**; actions are HTTP verbs, not path segments:

```text
GET    /api/v1/invoices
POST   /api/v1/invoices
GET    /api/v1/invoices/{invoice_id}
PATCH  /api/v1/invoices/{invoice_id}
DELETE /api/v1/invoices/{invoice_id}
```

For genuine operations that are not CRUD, a sub-resource verb is acceptable and
clearer than contorting the noun:

```text
POST /api/v1/invoices/{invoice_id}/send
POST /api/v1/invoices/{invoice_id}/void
```

Path parameters are named, never bare `{id}` — `{invoice_id}` reads correctly
in generated clients and in nested routes.

## Versioning

Version in the URL: `/api/v1/...`. Mounted once in
[`app/api.py`](../../app/api.py), never in a feature router.

URL versioning over header negotiation because it is visible in logs, in a
browser address bar and in a support ticket, and because it can be routed on at
the proxy.

**Versioning is a routing concern and must not leak into services.** When v2
arrives, both versions call the same service with different schemas mapped onto
it. If a service needs to know which version called it, the boundary is in the
wrong place.

Breaking changes require a new version. Adding an optional field or a new
endpoint is not breaking; removing a field, renaming one, tightening validation
or changing an error code is.

## Responses

Every success is wrapped:

```json
{ "data": { "id": "0193f4a2-...", "invoice_number": "INV-001" } }
```

```json
{
  "items": [ { "id": "0193f4a2-..." } ],
  "meta": { "page": 1, "size": 20, "total": 91, "pages": 5,
            "has_next": true, "has_previous": false }
}
```

Wrapping costs four characters and buys the ability to add top-level metadata —
deprecation notices, warnings, rate-limit state — without breaking clients that
already parse the response. A bare array cannot grow.

Status codes:

| Code | When |
| --- | --- |
| 200 | Successful read or update |
| 201 | Created — include a `Location` header |
| 202 | Accepted for asynchronous processing |
| 204 | Success with genuinely nothing to return |
| 400 | Malformed request the schema could not parse |
| 401 | No or invalid credentials |
| 403 | Authenticated but not permitted |
| 404 | Not found, or not visible to this caller |
| 409 | Conflict or business rule violation |
| 422 | Well-formed but failed validation |
| 429 | Rate limited — always include `Retry-After` |

## Errors

One shape, always. See [`error-handling.md`](error-handling.md).

```json
{
  "error": {
    "code": "invoice_not_found",
    "message": "Invoice not found.",
    "details": { "fields": [ { "field": "email", "message": "..." } ] },
    "request_id": "81687c27b18d4b259c63f893d9f5ec20"
  }
}
```

`code` is the field clients branch on and is **part of the public contract**.
Adding one is safe; changing one is a breaking change.

## Lists

Every collection endpoint is paginated. There is no unbounded list — a
collection that grows with tenant data will eventually time out.

```text
GET /api/v1/invoices?page=2&size=20&sort_by=created_at&order=desc&status=paid
```

- `page` is 1-based; `size` is capped at `MAX_PAGE_SIZE` (100) by validation.
- `sort_by` must be on the resource's explicit allow-list — see
  [`app/common/sorting.py`](../../app/common/sorting.py). Resolving arbitrary
  names against the model would expose every column, including private ones.
- Sorting always includes a unique tiebreaker. Without it, paging over a
  non-unique key duplicates and skips rows between requests.
- Filters are declared as a typed schema with `extra="forbid"`, so `?statuss=`
  is a 422 rather than a silently unfiltered response.

Use cursor pagination (`CursorPage`) for feeds and exports: offset pagination
makes the database walk every skipped row, and it shifts under concurrent
inserts.

## Requests

`PATCH` for partial updates, with all fields optional. Use `exclude_unset` so
"field omitted" stays distinguishable from "field explicitly set to null".

`PUT` only for genuine whole-resource replacement — rare, and usually the wrong
choice.

Never accept `id`, `tenant_id`, `created_at` or `updated_at` in a request body.
Schemas use `extra="forbid"`, so a client that sends one gets a 422 instead of a
cross-tenant write.

## Timestamps and money

All timestamps are UTC, ISO 8601, timezone-aware: `2026-01-15T10:30:00Z`.
Naive datetimes must never cross a layer boundary.

Money is a decimal string with an explicit currency, never a float:

```json
{ "amount": "1234.56", "currency": "USD" }
```

Binary floats cannot represent decimal fractions exactly, and the error
accumulates across a ledger.

## Idempotency

Unsafe operations that a client may retry should accept an `Idempotency-Key`
header, store the result against it, and return the stored result on a repeat.
Without it, a network timeout on `POST /payments` leaves the client unable to
distinguish "not charged" from "charged, response lost".

## Documentation

- Every endpoint has a `summary` and a docstring — the docstring becomes the
  description in the schema.
- `response_model` on every route, so responses are validated and documented.
- Operation IDs are generated as `<tag>_<function>` and become method names in
  generated clients. They must not change when a route moves.
- Add response examples. A schema shows the shape; an example shows a real
  payload, and integrators read the example first.
