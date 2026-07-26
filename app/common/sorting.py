"""Sorting parameters and safe column resolution.

Why this file exists
--------------------
Sorting looks like the most harmless query parameter in an API and is one of
the most dangerous. ``?sort_by=`` arrives as an arbitrary client string, and
the obvious implementation interpolates it into an ``ORDER BY`` clause — which
is SQL injection with extra steps. Even parameterised, ``getattr(Model,
sort_by)`` lets a caller sort by a column they cannot read, and sort order is
enough to binary-search a hidden value out of a table.

The rule this module enforces: **a sortable field is an explicit allow-list
entry, never a lookup on the model.** Anything not on the list is a 422.

There is a correctness reason too. Pagination over an unstable sort silently
duplicates and skips rows between pages, because rows that tie can come back in
any order. :func:`build_order_by` appends the primary key as a final tiebreaker
so the total ordering is deterministic.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.common.enums import SortOrder
from app.core.exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class SortableField:
    """One entry in a resource's sort allow-list.

    Attributes:
        api_name: The name clients send. Kept separate from the column so a
            column can be renamed without breaking the public API.
        column: The mapped ORM attribute to sort by.
    """

    api_name: str
    column: Any


class SortParams(BaseModel):
    """Sort field and direction, as received from the client.

    Declare as a router dependency so the parameters are validated and appear
    in the OpenAPI schema::

        sort: Annotated[SortParams, Depends()]
    """

    sort_by: str | None = Field(
        default=None,
        description="Field to sort by. Must be one of the resource's sortable fields.",
    )
    order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")


def build_order_by(
    params: SortParams,
    allowed: dict[str, Any],
    *,
    default: Any,
    tiebreaker: Any,
) -> list[Any]:
    """Resolve sort parameters into ORM order-by clauses.

    Args:
        params: The client's requested sort.
        allowed: Allow-list mapping the public field name to a mapped column.
            Built explicitly by each repository — never derived from the model,
            which would expose every column including internal ones.
        default: Clause used when the client requests no sort.
        tiebreaker: Unique column (normally the primary key) appended to
            guarantee a total ordering. Without it, paginating over a
            non-unique sort key duplicates and skips rows.

    Returns:
        Order-by clauses, ready to pass to ``select(...).order_by(*clauses)``.

    Raises:
        ValidationError: When the requested field is not on the allow-list. The
            error lists the valid options, because a 422 that does not say what
            is allowed forces the client author to guess.
    """
    if params.sort_by is None:
        return [default, tiebreaker]

    column = allowed.get(params.sort_by)
    if column is None:
        raise ValidationError(
            f"Cannot sort by '{params.sort_by}'.",
            code="invalid_sort_field",
            details={"allowed": sorted(allowed)},
        )

    clause = column.desc() if params.order is SortOrder.DESC else column.asc()
    return [clause, tiebreaker]
