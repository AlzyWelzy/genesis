"""Application error hierarchy and global exception handlers.

Why this file exists
--------------------
Two things go wrong when error handling is left to each endpoint. First, the
response shape drifts — some routes return ``{"detail": ...}``, others
``{"error": ...}`` — and clients end up with per-endpoint parsing. Second,
services start importing ``fastapi.HTTPException``, which couples business
logic to the transport layer and makes it unusable from a worker or a CLI.

The rule this module enforces:

* **Services raise domain errors** (subclasses of :class:`AppError`). They say
  *what* went wrong, never which HTTP status expresses it.
* **This module translates** them into a single documented envelope at the edge.

Adding an error type means adding a subclass — here when it is generic, or in a
module's ``exceptions.py`` when it is feature-specific — never a new handler.

The envelope
------------
Every failure, without exception::

    {
      "error": {
        "code": "not_found",
        "message": "Resource not found.",
        "details": {...},          // optional
        "request_id": "01H..."     // for support correlation
      }
    }

``code`` is the field clients branch on and is part of the public API contract:
adding one is safe, changing one is a breaking change.

What the client is told
-----------------------
Expected errors carry a message written for the caller. Unexpected ones carry
nothing but a request ID — the traceback goes to the logs. Leaking exception
text is how connection strings and file paths end up in a customer's console.
See ``docs/architecture/error-handling.md``.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for every expected, domain-level failure.

    Transport-agnostic by design: it carries a machine-readable ``code``, a
    human-readable ``message`` and optional structured ``details``. The HTTP
    status is a class attribute read only by the handler below, so the same
    exception can surface over HTTP, in a queue consumer or in a CLI.

    Attributes:
        status_code: HTTP status used when this error reaches an HTTP boundary.
        code: Stable identifier clients may branch on. Never change one in
            place — it is part of the public API contract.
        message: Safe-to-display description. Must not leak internals.
        headers: Extra response headers, e.g. ``Retry-After`` on a 429.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)


class ValidationError(AppError):
    """Input is well-formed but violates a business rule.

    Distinct from :class:`fastapi.exceptions.RequestValidationError`, which
    covers schema-level failures caught before a service is ever called. This
    one means the request parsed fine and the *domain* rejected it — "delivery
    date is before the order date", not "delivery_date is not a date".
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "Validation failed."


class BusinessRuleError(AppError):
    """An invariant of the domain would be violated.

    The distinction from :class:`ValidationError` is *whose* rule was broken.
    Validation is about the shape and consistency of the input; a business rule
    is about the state of the system: "this subscription cannot be downgraded
    mid-cycle", "an invoice cannot be voided after payment".

    Worth its own type because these are the errors product owners care about,
    and because the message is usually shown verbatim to an end user.
    """

    status_code = status.HTTP_409_CONFLICT
    code = "business_rule_violation"
    message = "Operation not permitted in the current state."


class NotFoundError(AppError):
    """A requested resource does not exist, or the caller may not see it.

    Deliberately ambiguous between the two. Distinguishing "does not exist"
    from "exists but is not yours" tells an attacker which IDs are real —
    enumeration through 403-vs-404 is a standard reconnaissance technique.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    """The request collides with current state: uniqueness, version, or a race.

    Typically raised when a unique constraint fires or an optimistic-lock
    version check fails. A conflict is usually retryable by the client after
    refetching; that is what distinguishes it from a business rule violation.
    """

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource conflict."


class AuthenticationError(AppError):
    """The caller's identity could not be established.

    Missing, malformed, expired or revoked credentials. Always carries a
    ``WWW-Authenticate`` header, because a 401 without one is not a valid HTTP
    challenge and confuses conforming clients.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Authentication required."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class AuthorizationError(AppError):
    """The caller is known but not permitted to perform this action.

    The 401/403 split is precise and worth keeping straight: 401 means "I do
    not know who you are", 403 means "I know exactly who you are, and no".
    Retrying with the same credentials will never help.

    Prefer :class:`NotFoundError` when even the resource's *existence* is
    privileged information.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "Permission denied."


class RateLimitError(AppError):
    """The caller exceeded an allowance.

    Always sets ``Retry-After``. Without it a client's only sane strategy is to
    retry blindly, which is precisely the behaviour the limit exists to stop.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests."

    def __init__(
        self, message: str | None = None, *, retry_after: int = 60, **kwargs: Any
    ) -> None:
        headers = kwargs.pop("headers", {})
        headers.setdefault("Retry-After", str(retry_after))
        super().__init__(message, headers=headers, **kwargs)


class ServiceUnavailableError(AppError):
    """A dependency the request needs is unreachable or degraded.

    For an outage in something the application talks to, not a bug in the
    application. Distinguishing them matters operationally: a 503 spike points
    at a dependency, a 500 spike points at a deploy.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service temporarily unavailable."


class ExternalServiceError(AppError):
    """An upstream third party returned an error or timed out.

    Separate from :class:`ServiceUnavailableError` so dashboards can tell "our
    Redis is down" from "the payment provider is having a bad day". They have
    different owners and different runbooks.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    message = "An upstream service failed."


def build_error_body(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the one and only error envelope shape the API emits.

    The request ID is attached automatically so a user can quote it to support
    and land on the exact log line, without the API ever exposing internals.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    if request_id := get_request_id():
        error["request_id"] = request_id
    return {"error": error}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render an error envelope as a JSON response."""
    return JSONResponse(
        status_code=status_code,
        content=build_error_body(code, message, details),
        headers=headers or None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the global handlers that normalise every error response.

    Args:
        app: The application to configure.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Domain errors are expected — log without a traceback.

        Server-side codes (5xx) are logged at ERROR because they need someone
        to look; client-side codes (4xx) at INFO because they are the API
        working as designed. Logging a 404 at ERROR is how alert fatigue starts.
        """
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("Domain error: %s", exc.message, extra={"code": exc.code})
        else:
            logger.info("Client error: %s", exc.message, extra={"code": exc.code})
        return _error_response(
            exc.status_code, exc.code, exc.message, exc.details, exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Schema violations, reshaped into the standard envelope.

        Field errors are passed through: they describe the caller's own request,
        so they leak nothing, and without them the client cannot tell which
        field was wrong.
        """
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Request validation failed.",
            {
                "fields": [
                    {
                        "field": ".".join(str(part) for part in error["loc"][1:]),
                        "message": error["msg"],
                        "type": error["type"],
                    }
                    for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Framework-raised errors (404 routing, 405) in the same envelope."""
        codes = {
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
            status.HTTP_401_UNAUTHORIZED: "unauthenticated",
            status.HTTP_403_FORBIDDEN: "permission_denied",
        }
        return _error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        """Anything unhandled is a bug: log the traceback, tell the client nothing.

        The response carries only a request ID. That is enough for a user to
        report the failure and for support to find the exact traceback, and not
        enough for anyone to learn anything about the internals.
        """
        logger.exception("Unhandled exception", exc_info=exc)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )
