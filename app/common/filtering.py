"""Filtering primitives for list endpoints.

Why this file exists
--------------------
Filtering is where list endpoints go wrong in three predictable ways, and all
three are cheaper to prevent once here than to fix per feature later.

**Injection.** A generic ``?filter=status eq active`` mini-language is a parser
that must be perfect forever. Instead, each resource declares a typed filter
schema — Pydantic validates it, and unknown fields are rejected before any
query is built.

**Unindexed scans.** ``?name_contains=`` becomes ``LIKE '%…%'``, which cannot
use a B-tree index. It is fine on ten thousand rows and takes the database down
at ten million. Repositories must declare which fields are searchable and back
them with the right index.

**Filtering as an access-control bypass.** A filter that reaches a column the
caller may not read leaks its contents one query at a time. Tenant scoping is
applied by the repository *after* filters and is never expressible as one.

The pattern
-----------
Each feature declares its own filter schema inheriting :class:`BaseFilter`, and
its repository translates the populated fields into query conditions
explicitly. Verbose on purpose: every filter that reaches the database was
written by hand and reviewed.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BaseFilter(BaseModel):
    """Base class for a resource's filter parameters.

    ``extra="forbid"`` is the important part: an unrecognised query parameter
    is a 422 rather than being silently ignored. A client filtering on
    ``?statuss=active`` should be told the field is wrong, not handed the
    unfiltered collection and left to believe it worked.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    def active_filters(self) -> dict[str, Any]:
        """Return only the fields the client actually supplied.

        Uses ``exclude_unset`` rather than ``exclude_none`` so that explicitly
        passing ``null`` stays distinguishable from omitting the field — which
        matters for nullable columns, where "has no value" is a real query.
        """
        return self.model_dump(exclude_unset=True)


class TimeRangeFilter(BaseFilter):
    """Common created-between filter, reusable across resources.

    Bounds are half-open: ``created_after`` is inclusive, ``created_before`` is
    exclusive. Two closed bounds either double-count or drop the boundary
    instant, which is how "daily totals" end up not summing to the monthly one.
    """

    created_after: datetime | None = Field(
        default=None, description="Inclusive lower bound on creation time (UTC)."
    )
    created_before: datetime | None = Field(
        default=None, description="Exclusive upper bound on creation time (UTC)."
    )


class SearchFilter(BaseFilter):
    """Free-text search parameter.

    Deliberately minimal. Anything beyond a simple prefix or trigram match
    belongs behind a search abstraction (PostgreSQL full-text to begin with);
    growing this into a query language is how an unindexed ``LIKE '%…%'`` ends
    up in the hot path.
    """

    q: str | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        description="Free-text search term.",
    )


# TODO: add a `FilterSpec` helper mapping a filter field to a column and an
# operator, so repositories can declare translations in a table instead of an
# if-chain — but only once three modules have proven they need the same shapes.
# Building the abstraction first guarantees it fits none of them.
