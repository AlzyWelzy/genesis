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

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, Self

from pydantic import BaseModel, Field

from app.common.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.common.enums import SortOrder
from app.core.exceptions import ValidationError


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

    @classmethod
    def build(cls, items: Sequence[T], *, next_cursor: str | None = None) -> Self:
        """Assemble a cursor page."""
        return cls(items=list(items), next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Cursor encoding
# ---------------------------------------------------------------------------
#
# A cursor is the sort-key values of the last row on a page, encoded so clients
# treat it as opaque. Three properties matter, and each is a real failure if
# missing:
#
# * **Opaque** — a client that parses a cursor will depend on its structure,
#   and the structure is an implementation detail that must stay changeable.
# * **Versioned** — a cursor minted before a sort-order change is meaningless
#   afterwards. The version lets it be rejected cleanly rather than silently
#   returning the wrong window.
# * **Tamper-evident** — an HMAC stops a client from forging a cursor to probe
#   arbitrary key ranges. Base64 alone is encoding, not protection.


#: Bumped whenever the cursor payload's meaning changes. An older cursor is
#: then rejected with a clear error instead of paging from the wrong place.
#:
#: v2 changed the wire format: the signature is now appended with no separator
#: and split off by its fixed length. v1 used a ``.`` between payload and
#: signature and recovered the split with ``rpartition``, which finds the *last*
#: dot — and the signature is raw bytes, so roughly one signature in sixteen
#: contains ``0x2e`` and was split in the wrong place. Around 7% of cursors were
#: therefore rejected as forged, at random, and a client paginating hit a
#: spurious "invalid cursor" about one page in fourteen.
CURSOR_VERSION: Final[int] = 2

#: Bytes of HMAC kept. 128 bits is far beyond what forging a pagination cursor
#: is worth, and keeps the cursor short enough to sit in a URL comfortably.
#: Fixed, which is what lets the signature be split off by length.
_SIGNATURE_BYTES: Final[int] = 16


def encode_cursor(values: Mapping[str, Any], *, secret: str) -> str:
    """Encode sort-key values into an opaque, signed cursor.

    Args:
        values: The last row's sort-key values, e.g.
            ``{"created_at": "2026-01-15T10:30:00+00:00", "id": "0193..."}``.
            Must be JSON-representable.
        secret: Signing secret. Use the application's signing key material, not
            a literal.

    Returns:
        A URL-safe cursor string.
    """
    payload = json.dumps(
        {"v": CURSOR_VERSION, "k": dict(values)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    # Concatenated with no separator. Any delimiter byte can also occur inside
    # the signature, which is raw HMAC output — and then splitting on the
    # delimiter finds the wrong boundary and rejects a cursor we ourselves just
    # minted. The signature has a fixed length, so the boundary is known without
    # marking it.
    signature = _sign(payload, secret)
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def _sign(payload: bytes, secret: str) -> bytes:
    """Return the truncated HMAC for a cursor payload."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()[
        :_SIGNATURE_BYTES
    ]


def decode_cursor(cursor: str, *, secret: str) -> dict[str, Any]:
    """Decode and verify a cursor produced by :func:`encode_cursor`.

    Args:
        cursor: The cursor supplied by the client.
        secret: The same signing secret used to encode it.

    Returns:
        The sort-key values.

    Raises:
        ValidationError: When the cursor is malformed, forged, or was minted
            for an older cursor version. All three are reported as one generic
            "invalid cursor" so a client cannot distinguish a signature failure
            from a decode failure and probe the format.
    """
    values = _decode_cursor_payload(cursor, secret)
    if values is None:
        raise ValidationError("Invalid or expired cursor.", code="invalid_cursor")
    return values


def _decode_cursor_payload(cursor: str, secret: str) -> dict[str, Any] | None:
    """Decode and verify a cursor, returning ``None`` if it is not usable.

    Split out so the caller raises once. Every failure mode collapses to
    ``None`` on purpose: distinguishing "bad signature" from "bad base64" from
    "stale version" would let a client probe the cursor format.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
    except ValueError, TypeError:
        return None

    if len(raw) <= _SIGNATURE_BYTES:
        return None
    payload, signature = raw[:-_SIGNATURE_BYTES], raw[-_SIGNATURE_BYTES:]

    # Constant time: a byte-by-byte comparison that returns early leaks how much
    # of a forged signature was correct, which is enough to build one a byte at
    # a time.
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return None

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None

    if not isinstance(decoded, dict) or decoded.get("v") != CURSOR_VERSION:
        return None
    keys = decoded.get("k")
    return keys if isinstance(keys, dict) else None
