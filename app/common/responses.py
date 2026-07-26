"""Standard response envelopes.

Why this file exists
--------------------
Clients integrate against a *shape*, not against individual endpoints. If one
route returns a bare object, another wraps it in ``{"data": ...}`` and a third
adds a ``success`` flag, every consumer needs per-endpoint handling and the
OpenAPI schema stops being a useful contract.

These generics fix the shape once. Errors are produced by
:mod:`app.core.exceptions`, which emits the matching error envelope, so success
and failure stay symmetrical.

Usage::

    @router.get("/things/{id}", response_model=DataResponse[ThingRead])
    async def get_thing(...) -> DataResponse[ThingRead]:
        ...
"""

from typing import Any, Self

from pydantic import BaseModel, Field

from app.common.types import BaseSchema


class DataResponse[T](BaseModel):
    """Envelope wrapping a single resource.

    Wrapping rather than returning the object bare leaves room to add
    top-level metadata (warnings, deprecation notices, request IDs) later
    without breaking every client.
    """

    data: T

    @classmethod
    def of(cls, data: T) -> Self:
        """Construct an envelope around ``data``."""
        return cls(data=data)


class ListResponse[T](BaseModel):
    """Envelope wrapping an unpaginated collection.

    Only for collections with a small, bounded size (enum options, a user's
    roles). Anything that grows with tenant data must use
    :class:`app.common.pagination.Page` — an unbounded list will eventually
    time out or exhaust memory.
    """

    data: list[T]
    count: int = Field(description="Number of items in `data`.")

    @classmethod
    def of(cls, items: list[T]) -> Self:
        """Construct an envelope around ``items``."""
        return cls(data=items, count=len(items))


class MessageResponse(BaseSchema):
    """Acknowledgement carrying no resource.

    For operations whose result is the absence of an error — a triggered
    re-send, an accepted background job. Prefer ``204 No Content`` when there
    is genuinely nothing to say.
    """

    message: str
    details: dict[str, Any] | None = None


class ErrorDetail(BaseSchema):
    """Body of an error response.

    Declared here so it can be referenced in ``responses={...}`` and appear in
    the OpenAPI schema. It is *emitted* by the handlers in
    :mod:`app.core.exceptions`; the two must be kept in step.
    """

    code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable, safe-to-display summary.")
    details: dict[str, Any] | None = Field(
        default=None, description="Structured context, e.g. per-field errors."
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "Identifier of the request that failed. Quote this to support: it "
            "locates the exact log line without the API exposing internals."
        ),
    )


class ErrorResponse(BaseSchema):
    """Top-level error envelope: ``{"error": {...}}``."""

    error: ErrorDetail


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Build a ``responses=`` mapping documenting the given error statuses.

    The application already attaches a global set to every operation (see
    :data:`app.core.openapi.COMMON_ERROR_RESPONSES`). Use this when a specific
    endpoint needs to document a status the global set omits, or to narrow the
    documented set for an endpoint that genuinely cannot produce some of them.

    Documented errors matter more than they look: a generated client only
    handles the envelope if the schema says the envelope exists. Undocumented,
    the client assumes 2xx is the only shape and crashes on the first 409.

    Usage::

        @router.post("/invoices", responses=error_responses(409, 422))

    Args:
        *statuses: HTTP status codes to document.

    Returns:
        A mapping suitable for FastAPI's ``responses`` parameter.

    Raises:
        KeyError: When a status has no registered description — better than
            silently documenting nothing.
    """
    return {status: ERROR_RESPONSE_DESCRIPTIONS[status] for status in statuses}


#: Descriptions for every error status the API can emit. One source of truth,
#: consumed by both the global default and :func:`error_responses`.
ERROR_RESPONSE_DESCRIPTIONS: dict[int, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed request."},
    401: {"model": ErrorResponse, "description": "Authentication required."},
    403: {"model": ErrorResponse, "description": "Permission denied."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    409: {"model": ErrorResponse, "description": "Conflict with current state."},
    422: {"model": ErrorResponse, "description": "Validation failed."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": ErrorResponse, "description": "Unexpected server error."},
    502: {"model": ErrorResponse, "description": "Upstream service failed."},
    503: {"model": ErrorResponse, "description": "Service temporarily unavailable."},
}
