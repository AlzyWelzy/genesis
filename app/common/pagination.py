"""Pagination primitives.

Why this file exists
--------------------
Every list endpoint in a SaaS platform needs paging, and every one of them
would otherwise invent its own parameter names, defaults and response shape.
Beyond consistency there is a correctness argument: an endpoint without an
enforced maximum page size is a denial-of-service vector, and one that forgets
``ORDER BY`` returns non-deterministic pages where rows are duplicated or
skipped between requests.

The types here are transport-agnostic — they contain no SQLAlchemy. Applying
them to a query is the repository's job, which keeps this module usable from a
worker or a cache layer.

Offset vs cursor
----------------
Offset paging is provided because it is what UIs with page numbers need. It
degrades badly at depth (``OFFSET 100000`` makes the database walk 100,000
rows) and can skip or repeat rows when the underlying data changes between
requests. Cursor paging avoids both and is sketched below for feeds and exports.
"""

from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, Field

from app.common.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.common.enums import SortOrder


class PaginationParams(BaseModel):
    """Query parameters for an offset-paginated endpoint.

    Declare as a router dependency so the constraints are enforced by
    validation and documented in OpenAPI::

        params: Annotated[PaginationParams, Depends()]
    """

    page: int = Field(default=1, ge=1, description="1-based page number.")
    size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="Items per page.",
    )

    @property
    def offset(self) -> int:
        """SQL ``OFFSET`` for this page."""
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        """SQL ``LIMIT`` for this page."""
        return self.size


class SortParams(BaseModel):
    """Sort field and direction.

    The field name arrives from the client and must never be interpolated into
    SQL. Repositories validate it against an explicit allow-list of sortable
    columns and reject anything else.
    """

    sort_by: str | None = Field(default=None, description="Field name to sort by.")
    order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")


class PageMeta(BaseModel):
    """Pagination metadata returned alongside a page of items."""

    page: int
    size: int
    total: int = Field(description="Total matching items across all pages.")
    pages: int = Field(description="Total number of pages.")
    has_next: bool
    has_previous: bool

    @classmethod
    def build(cls, *, page: int, size: int, total: int) -> Self:
        """Derive metadata from the page position and total count."""
        pages = (total + size - 1) // size if size else 0
        return cls(
            page=page,
            size=size,
            total=total,
            pages=pages,
            has_next=page < pages,
            has_previous=page > 1,
        )


class Page[T](BaseModel):
    """A page of items plus its metadata.

    The canonical return type of every list endpoint::

        @router.get("/things", response_model=Page[ThingRead])
    """

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: Sequence[T], *, params: PaginationParams, total: int) -> Self:
        """Assemble a page from a slice of items and the total count."""
        return cls(
            items=list(items),
            meta=PageMeta.build(page=params.page, size=params.size, total=total),
        )


class CursorParams(BaseModel):
    """Query parameters for cursor (keyset) pagination.

    Preferred for feeds, exports and any deep list: cost is independent of how
    far the client has scrolled, and concurrent inserts cannot shift the window.
    """

    cursor: str | None = Field(
        default=None, description="Opaque cursor from the previous page."
    )
    size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class CursorPage[T](BaseModel):
    """A cursor-paginated page.

    No total count: computing one requires a full scan, which is exactly the
    cost cursor pagination exists to avoid.
    """

    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor`; null means last page."
    )


# TODO: implement opaque cursor encode/decode (base64 of the sort key tuple)
# and sign or version them so a client cannot forge or replay one across a
# schema change.
