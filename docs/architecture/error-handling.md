# Error handling

## The rule

**Services raise domain errors. The edge translates them into HTTP.**

A service that raises `HTTPException` cannot be called from a worker, a CLI or
a test without dragging FastAPI in, and it has quietly become a router. The
split is:

```text
Service          raises  NotFoundError("Invoice not found.")
                          │  knows what went wrong
                          ▼
Exception handler maps to  404 + {"error": {"code": "not_found", ...}}
                          │  knows how HTTP expresses it
                          ▼
Client
```

## The hierarchy

All in [`app/core/exceptions.py`](../../app/core/exceptions.py), all inheriting
`AppError`.

| Exception | Status | Code | Means |
| --- | --- | --- | --- |
| `ValidationError` | 422 | `validation_error` | Input is well-formed but a domain rule rejects it |
| `BusinessRuleError` | 409 | `business_rule_violation` | An invariant would be violated by the current state |
| `NotFoundError` | 404 | `not_found` | Does not exist, or is not visible to this caller |
| `ConflictError` | 409 | `conflict` | Uniqueness, version or race collision |
| `AuthenticationError` | 401 | `unauthenticated` | Identity could not be established |
| `AuthorizationError` | 403 | `permission_denied` | Known caller, not permitted |
| `RateLimitError` | 429 | `rate_limited` | Allowance exceeded |
| `ServiceUnavailableError` | 503 | `service_unavailable` | Our dependency is down |
| `ExternalServiceError` | 502 | `external_service_error` | A third party failed |

### Distinctions worth getting right

**`ValidationError` vs `BusinessRuleError`** — whose rule was broken.
Validation is about the input ("delivery date is not a date"); a business rule
is about system state ("delivery date is before the order date", "a paid invoice
cannot be voided").

**401 vs 403** — 401 means "I do not know who you are", 403 means "I know
exactly who you are, and no". Retrying a 403 with the same credentials will
never help.

**403 vs 404** — when the *existence* of a resource is privileged, return 404.
Distinguishing them tells an attacker which IDs are real; enumeration through
403-vs-404 is standard reconnaissance.

**503 vs 502** — "our Redis is down" and "the payment provider is having a bad
day" have different owners and different runbooks. Keeping them apart makes a
dashboard actionable.

## Feature-specific errors

Subclass a base in the module's `exceptions.py`. Never register a new handler.

```python
class InvoiceNotFoundError(NotFoundError):
    """The requested invoice does not exist in this tenant."""

    code = "invoice_not_found"
    message = "Invoice not found."
```

The status comes from the base, so the global handler already knows what to do.

## The envelope

Every failure, without exception:

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

- **`code`** — what clients branch on. Part of the public API contract: adding
  one is safe, changing one is breaking.
- **`message`** — safe to display. Never contains internals.
- **`details`** — optional structured context. Field errors, allowed values.
- **`request_id`** — attached automatically from the request context. A user
  can quote it and support lands on the exact log line, without the API
  exposing anything.

## What the client is told

Expected errors carry a message written for the caller.

**Unexpected errors carry nothing.** The traceback goes to the logs; the
response is a generic message and a request ID. Leaking exception text is how
connection strings, file paths and library versions end up in a customer's
browser console.

```python
@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception", exc_info=exc)  # full detail here
    return _error_response(500, "internal_error", "An unexpected error occurred.")
```

## Log levels

Set by status, in the `AppError` handler:

- **5xx → ERROR.** Someone needs to look.
- **4xx → INFO.** The API working as designed. Logging a 404 at ERROR is how
  alert fatigue starts, and alert fatigue is how a real 500 gets ignored.
- **Unhandled → `logger.exception`.** With the traceback, always.

## Rules

**Never catch an exception you cannot handle.** `except Exception: pass`
converts a bug into corrupted state discovered days later.

**Never catch and re-raise as HTTP in a service.** That is the handler's job,
and doing it in a service is exactly the coupling this design removes.

**Do not use exceptions for control flow.** A repository returning `None` for a
missing row is correct — whether that is an error depends on the caller, and the
service decides.

**Attach context to the error, not to the log message.** `details` travels with
the exception; a log line written at the raise site does not reach the handler.

## Testing

Assert on `code` and `status_code`, never on message text — messages are
copy and will be reworded, while codes are contract.

```python
with pytest.raises(ValidationError) as exc_info:
    validate_password_policy("short")
assert exc_info.value.code == "password_policy_violation"
```
