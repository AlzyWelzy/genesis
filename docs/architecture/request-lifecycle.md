# Request lifecycle

What happens to an HTTP request, in order, and which file owns each step.

## The path

```text
 1. Uvicorn accepts the connection
 2. RequestContextMiddleware    mint request ID, adopt correlation ID
 3. TrustedHostMiddleware       reject forged Host headers
 4. CORSMiddleware              preflight, and CORS headers on every response
 5. AccessLogMiddleware         start timer
 6. RateLimitMiddleware         check the caller's allowance
 7. GZipMiddleware              (wraps the response on the way out)
 8. Routing                     match path → endpoint
 9. Dependencies                session → claims → user → tenant → permission
10. Router handler              parse and validate the request body
11. Service                     business rules, orchestration
12. Repository                  build and execute SQL
13. Service                     commit
14. Router handler              serialise the response schema
    ── unwind ──
15. GZip → access log → CORS → context reset → response sent
```

Middleware order is set in [`app/core/middleware.py`](../../app/core/middleware.py).
Starlette applies middleware **outside-in in reverse registration order**: the
one added last wraps all the others.

## Why the order is what it is

**Context first (outermost).** Every layer inside it — including the access log
and every exception handler — needs the request ID. Anything registered outside
it would log without one.

**CORS outside the application.** Two reasons, and the second is the one people
discover the hard way: preflight `OPTIONS` requests must be answered before
routing, *and* CORS headers must be attached to error responses too. A 500
without CORS headers appears in the browser as an opaque network failure with
no clue as to the cause.

**Access log inside CORS.** So the duration recorded is the application's own
work rather than including preflight handling.

**GZip innermost.** It compresses the final body, after every other layer has
finished with it.

## Context propagation

`RequestContextMiddleware` sets two `ContextVar`s that follow the request
through every layer without appearing in a single signature:

- `request_id` — minted per request, echoed as `X-Request-ID`.
- `correlation_id` — adopted from the inbound `X-Correlation-ID` when an
  upstream caller supplied one, so one user action shares an ID across
  services; otherwise it mirrors the request ID.

Both are attached to every log record by `ContextFilter`, and the request ID is
attached to every error response body. That is what turns "something failed" into
a user quoting an ID that lands on the exact log line.

Authentication dependencies later add `tenant_id` and `user_id` to the same
context. The repository layer reads `tenant_id` to scope every query — see
[`database-conventions.md`](database-conventions.md).

**Context is reset in a `finally` block.** `ContextVar` values set inside a task
outlive the request otherwise, and a worker reusing the task would inherit a
stale tenant — which in a multi-tenant system is a data leak, not a cosmetic bug.

## Where each concern belongs

| Concern | Where | Why not elsewhere |
| --- | --- | --- |
| Request ID | Middleware | Must cover every request, including 404s |
| Authentication | Dependency | Needs to appear in the OpenAPI schema; only costs what uses it |
| Authorization | Dependency | Declared at the route, visible next to what it guards |
| Tenant resolution | Dependency | Needs the authenticated user |
| Validation (shape) | Pydantic schema | Rejected before any handler runs |
| Validation (rules) | Service | Needs domain state, not just the payload |
| Transaction | Service | Only it knows whether N writes are one operation |
| Serialisation | Router | The only layer that should know about HTTP |

Middleware runs on **every** request including ones it is irrelevant to;
dependencies run only where declared and are individually testable and
overridable. Prefer a dependency unless the concern is genuinely universal.

## The transaction boundary

One session per request, provided by `get_session`, rolled back if the request
raises.

**The service commits. Repositories never do.** If a repository committed, a
service could not make two writes atomic — the first would already be durable
when the second failed.

```python
async def transfer(self, source_id: UUID, target_id: UUID, amount: Decimal) -> None:
    """Move funds between two accounts atomically."""
    source = await self.accounts.get(source_id)
    target = await self.accounts.get(target_id)
    # ... business rules ...
    await self.accounts.debit(source, amount)  # flush, no commit
    await self.accounts.credit(target, amount)  # flush, no commit
    await self.session.commit()  # one atomic operation
```

## Side effects after commit

Publish events and enqueue jobs **after** the commit, never before.

A job dispatched inside an open transaction can start executing before — or
despite — the commit, and will not find the row it was told about. This failure
is timing-dependent, so it passes every test and appears under production load.

```python
await self.session.commit()
await event_bus.publish(InvoicePaid(invoice_id=invoice.id))  # after
```

## Errors

Any `AppError` raised at any depth propagates to the handlers in
[`app/core/exceptions.py`](../../app/core/exceptions.py), which convert it into
the standard envelope. Services never catch an error just to re-raise it as
HTTP. See [`error-handling.md`](error-handling.md).
