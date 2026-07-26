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
* **This module translates** those errors into a single, documented response
  envelope at the edge.

Adding a new error type means adding a subclass here (or in a module's
``exceptions.py`` when it is feature-specific) — never a new handler.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for every expected, domain-level failure.

    Transport-agnostic by design: it carries a machine-readable ``code``, a
    human-readable ``message`` and optional structured ``details``. The HTTP
    status is an attribute of the subclass, read only by the handler below, so
    the same exception can be surfaced over HTTP, a queue consumer or a CLI.

    Attributes:
        status_code: HTTP status used when this error reaches an HTTP boundary.
        code: Stable identifier clients may branch on. Never change one
            in place — it is part of the public API contract.
        message: Safe-to-display description. Must not leak internals.
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
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    """A requested resource does not exist, or the caller may not see it."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    """The request collides with current state (uniqueness, version, race)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource conflict."


class ValidationError(AppError):
    """Input is well-formed but violates a business rule.

    Distinct from :class:`fastapi.exceptions.RequestValidationError`, which
    covers schema-level failures handled before a service is ever called.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "Validation failed."


class AuthenticationError(AppError):
    """The caller's identity could not be established."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Authentication required."


class PermissionDeniedError(AppError):
    """The caller is known but is not allowed to perform this action."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "Permission denied."


class RateLimitError(AppError):
    """The caller exceeded an allowance."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests."


class ServiceUnavailableError(AppError):
    """A dependency the request needs is unreachable or degraded."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service temporarily unavailable."


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the one and only error envelope shape the API emits."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    body: dict[str, Any] = {"error": error}
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Install the global handlers that normalise every error response.

    Args:
        app: The application to configure.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Domain errors are expected — log at WARNING, never with a traceback."""
        logger.warning("Domain error: %s (%s)", exc.message, exc.code)
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Schema violations, reshaped into the standard envelope."""
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Request validation failed.",
            {"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Framework-raised errors (404 routing, 405) in the same envelope."""
        return _error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        """Anything unhandled is a bug: log the traceback, tell the client nothing.

        Leaking exception text here is how stack traces and connection strings
        end up in client logs. The traceback goes to the server logs only.
        """
        logger.exception("Unhandled exception", exc_info=exc)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )

    # TODO: attach the request ID to every error body once the request-context
    # middleware exists, so support can correlate a client report to a log line.
