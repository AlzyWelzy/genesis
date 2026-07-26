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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ValidationError


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


class Operator(StrEnum):
    """How a filter value is compared to a column.

    A closed set on purpose. A generic operator language ("give me ``gt``,
    ``in``, ``regex``…") is a parser that must be perfect forever and an
    invitation to build query shapes no index supports. Each member here exists
    because a real list endpoint needs it.
    """

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    IS_NULL = "is_null"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """Declares how one filter field maps onto a column and an operator.

    Repositories declare a table of these instead of writing an if-chain, so
    adding a filter is one line and every filter goes through the same
    validated path. The table is still written by hand — nothing is derived
    from the model — which is what keeps a filter from reaching a column the
    caller may not read.

    Attributes:
        field: Name on the filter schema.
        column: Mapped ORM attribute to compare against.
        operator: How to compare.
        indexed: Whether an index supports this predicate. Declaring ``False``
            is a deliberate acknowledgement that the filter will scan; it is
            surfaced by :func:`unindexed_filters` so a review can catch the
            ``LIKE '%…%'`` that is fine on ten thousand rows and fatal on ten
            million.
    """

    field: str
    column: Any
    operator: Operator = Operator.EQ
    indexed: bool = True

    def build(self, value: Any) -> Any:  # noqa: PLR0911 - one branch per operator
        """Build the SQLAlchemy predicate for a supplied value.

        Args:
            value: The client-supplied filter value, already validated by the
                filter schema — so it is the right *type*, and this method only
                decides the comparison.

        Returns:
            A SQLAlchemy binary expression.

        Raises:
            ValueError: When the operator is not supported for the value given
                (an empty ``IN``, for example, which would silently match
                nothing rather than being an obvious mistake).
        """
        match self.operator:
            case Operator.EQ:
                return self.column == value
            case Operator.NE:
                return self.column != value
            case Operator.LT:
                return self.column < value
            case Operator.LTE:
                return self.column <= value
            case Operator.GT:
                return self.column > value
            case Operator.GTE:
                return self.column >= value
            case Operator.IN:
                if not value:
                    raise ValueError(f"Filter {self.field!r} needs a non-empty list")
                return self.column.in_(value)
            case Operator.CONTAINS:
                # Escaped so a value containing % or _ is matched literally
                # rather than becoming a wildcard the caller did not intend.
                return self.column.ilike(f"%{_escape_like(value)}%", escape="\\")
            case Operator.STARTS_WITH:
                # Anchored, so a B-tree index on the column can still serve it.
                return self.column.ilike(f"{_escape_like(value)}%", escape="\\")
            case Operator.IS_NULL:
                return self.column.is_(None) if value else self.column.is_not(None)
            case _:  # pragma: no cover - the enum is closed
                raise ValueError(f"Unsupported operator: {self.operator}")


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a user's ``%`` is a literal percent sign."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_filters(filters: BaseFilter, specs: Sequence[FilterSpec]) -> list[Any]:
    """Translate a populated filter schema into SQLAlchemy predicates.

    Only fields the client actually supplied produce a predicate, so an absent
    filter never becomes ``WHERE column IS NULL``.

    Args:
        filters: The validated filter schema.
        specs: The repository's declared translations.

    Returns:
        Predicates to pass to ``select(...).where(*predicates)``.

    Raises:
        ValidationError: When a supplied field has no spec. That is a
            programming error — the schema and the spec table have drifted —
            and it must not silently return unfiltered data.
    """
    supplied = filters.active_filters()
    by_field = {spec.field: spec for spec in specs}

    predicates: list[Any] = []
    for field, value in supplied.items():
        if value is None:
            continue
        spec = by_field.get(field)
        if spec is None:
            raise ValidationError(
                f"Filter {field!r} is not supported for this resource.",
                code="invalid_filter",
                details={"allowed": sorted(by_field)},
            )
        predicates.append(spec.build(value))
    return predicates


def unindexed_filters(specs: Sequence[FilterSpec]) -> list[str]:
    """Return the fields whose filters are declared as unindexed.

    For a startup assertion or an architecture test. A filter that scans is
    sometimes the right call; one that scans *by accident* is how a list
    endpoint works perfectly in staging and times out in production.
    """
    return [spec.field for spec in specs if not spec.indexed]
