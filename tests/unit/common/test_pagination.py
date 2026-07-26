"""Tests for pagination and sort-parameter safety.

The sort allow-list test is a security test, not an ergonomics one: resolving a
client-supplied field name against the model would expose every column,
including ones the caller cannot read.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.common.constants import MAX_PAGE_SIZE
from app.common.enums import SortOrder
from app.common.pagination import Page, PageMeta, PaginationParams
from app.common.sorting import SortParams, build_order_by
from app.core.exceptions import ValidationError


class TestPaginationParams:
    """Bounds enforced by validation rather than by the repository."""

    def test_offset_and_limit_derive_from_page(self) -> None:
        params = PaginationParams(page=3, size=20)
        assert params.offset == 40
        assert params.limit == 20

    def test_first_page_has_zero_offset(self) -> None:
        assert PaginationParams(page=1, size=20).offset == 0

    def test_size_above_maximum_is_rejected(self) -> None:
        """An unbounded page size is a denial-of-service vector."""
        with pytest.raises(PydanticValidationError):
            PaginationParams(page=1, size=MAX_PAGE_SIZE + 1)

    def test_page_below_one_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            PaginationParams(page=0, size=20)


class TestPageMeta:
    """Derived pagination metadata."""

    @pytest.mark.parametrize(
        ("page", "size", "total", "pages", "has_next", "has_previous"),
        [
            (1, 20, 0, 0, False, False),
            (1, 20, 5, 1, False, False),
            (1, 20, 41, 3, True, False),
            (2, 20, 41, 3, True, True),
            (3, 20, 41, 3, False, True),
        ],
    )
    def test_metadata_is_derived_correctly(
        self,
        page: int,
        size: int,
        total: int,
        pages: int,
        has_next: bool,
        has_previous: bool,
    ) -> None:
        meta = PageMeta.build(page=page, size=size, total=total)
        assert (meta.pages, meta.has_next, meta.has_previous) == (
            pages,
            has_next,
            has_previous,
        )

    def test_partial_final_page_is_counted(self) -> None:
        """41 items at 20 per page is three pages, not two."""
        assert PageMeta.build(page=1, size=20, total=41).pages == 3

    def test_page_wraps_items_with_metadata(self) -> None:
        page = Page.build(["a", "b"], params=PaginationParams(page=1, size=20), total=2)
        assert page.items == ["a", "b"]
        assert page.meta.total == 2


class TestSortSafety:
    """Sort fields must come from an explicit allow-list."""

    def test_unknown_field_is_rejected(self) -> None:
        """Resolving arbitrary names against the model would expose every column."""
        with pytest.raises(ValidationError) as exc_info:
            build_order_by(
                SortParams(sort_by="password_hash"),
                allowed={"created_at": object()},
                default=object(),
                tiebreaker=object(),
            )
        assert exc_info.value.code == "invalid_sort_field"

    def test_rejection_lists_the_allowed_fields(self) -> None:
        """A 422 that does not say what is valid forces the client to guess."""
        with pytest.raises(ValidationError) as exc_info:
            build_order_by(
                SortParams(sort_by="nope"),
                allowed={"created_at": object(), "name": object()},
                default=object(),
                tiebreaker=object(),
            )
        assert exc_info.value.details["allowed"] == ["created_at", "name"]

    def test_default_sort_still_includes_a_tiebreaker(self) -> None:
        """Without a unique tiebreaker, paging duplicates and skips rows."""
        default, tiebreaker = object(), object()
        clauses = build_order_by(
            SortParams(), allowed={}, default=default, tiebreaker=tiebreaker
        )
        assert clauses == [default, tiebreaker]

    def test_default_order_is_descending(self) -> None:
        assert SortParams().order is SortOrder.DESC
