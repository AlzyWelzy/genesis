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


# TODO: add a shared `responses={401: ..., 403: ..., 422: ...}` mapping that
# routers can spread, so the documented error set is consistent everywhere.
