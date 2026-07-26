"""Tests for filter translation.

Filtering is where a list endpoint most easily becomes either an injection
vector or an unindexed full-table scan. The tests below are weighted towards
those two failures rather than towards the happy path.
"""

import pytest
from sqlalchemy import Column, Integer, String, Table
from sqlalchemy import MetaData as SAMetaData

from app.common.filtering import (
    BaseFilter,
    FilterSpec,
    Operator,
    SearchFilter,
    TimeRangeFilter,
    build_filters,
    unindexed_filters,
)
from app.core.exceptions import ValidationError

_metadata = SAMetaData()
_things = Table(
    "things",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("status", String),
    Column("count", Integer),
)


class ThingFilter(BaseFilter):
    name: str | None = None
    status: str | None = None
    count: int | None = None


SPECS = [
    FilterSpec("name", _things.c.name, Operator.CONTAINS, indexed=False),
    FilterSpec("status", _things.c.status, Operator.EQ),
    FilterSpec("count", _things.c.count, Operator.GTE),
]


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


class TestOperators:
    @pytest.mark.parametrize(
        ("operator", "value", "fragment"),
        [
            (Operator.EQ, "active", "things.status = 'active'"),
            (Operator.NE, "active", "things.status != 'active'"),
            (Operator.IN, ["a", "b"], "IN ('a', 'b')"),
            (Operator.IS_NULL, True, "things.status IS NULL"),
            (Operator.IS_NULL, False, "things.status IS NOT NULL"),
        ],
    )
    def test_builds_the_expected_predicate(
        self, operator: Operator, value, fragment: str
    ) -> None:
        spec = FilterSpec("status", _things.c.status, operator)
        assert fragment in _sql(spec.build(value))

    @pytest.mark.parametrize(
        ("operator", "fragment"),
        [
            (Operator.LT, "count < 5"),
            (Operator.LTE, "count <= 5"),
            (Operator.GT, "count > 5"),
            (Operator.GTE, "count >= 5"),
        ],
    )
    def test_comparison_operators(self, operator: Operator, fragment: str) -> None:
        spec = FilterSpec("count", _things.c.count, operator)
        assert fragment in _sql(spec.build(5))

    def test_empty_in_is_rejected(self) -> None:
        """An empty IN matches nothing, silently — almost never intended."""
        spec = FilterSpec("status", _things.c.status, Operator.IN)
        with pytest.raises(ValueError, match="non-empty"):
            spec.build([])


class TestLikeEscaping:
    def test_percent_is_escaped(self) -> None:
        """Otherwise a user searching for "100%" matches everything."""
        spec = FilterSpec("name", _things.c.name, Operator.CONTAINS)
        assert r"100\%" in _sql(spec.build("100%"))

    def test_underscore_is_escaped(self) -> None:
        """`_` is a single-character wildcard in LIKE."""
        spec = FilterSpec("name", _things.c.name, Operator.CONTAINS)
        assert r"a\_b" in _sql(spec.build("a_b"))

    def test_backslash_is_escaped(self) -> None:
        spec = FilterSpec("name", _things.c.name, Operator.CONTAINS)
        assert _sql(spec.build("a\\b"))

    def test_starts_with_is_anchored(self) -> None:
        """Anchoring is what lets a B-tree index still serve the query."""
        spec = FilterSpec("name", _things.c.name, Operator.STARTS_WITH)
        sql = _sql(spec.build("pre"))
        assert "'pre%'" in sql
        assert "'%pre" not in sql


class TestBuildFilters:
    def test_only_supplied_fields_produce_predicates(self) -> None:
        """An absent filter must not become `WHERE column IS NULL`."""
        predicates = build_filters(ThingFilter(status="active"), SPECS)
        assert len(predicates) == 1

    def test_no_filters_produces_nothing(self) -> None:
        assert build_filters(ThingFilter(), SPECS) == []

    def test_several_filters_combine(self) -> None:
        predicates = build_filters(ThingFilter(status="active", count=3), SPECS)
        assert len(predicates) == 2

    def test_an_unspecced_field_is_rejected(self) -> None:
        """Schema and spec table drifting must not silently return everything."""

        class Wider(BaseFilter):
            secret_column: str | None = None

        with pytest.raises(ValidationError) as exc:
            build_filters(Wider(secret_column="x"), SPECS)
        assert exc.value.code == "invalid_filter"

    def test_rejection_lists_the_allowed_fields(self) -> None:
        class Wider(BaseFilter):
            nope: str | None = None

        with pytest.raises(ValidationError) as exc:
            build_filters(Wider(nope="x"), SPECS)
        assert exc.value.details["allowed"] == ["count", "name", "status"]


class TestSchemas:
    def test_unknown_query_parameters_are_rejected(self) -> None:
        """A client typo must be a 422, not a silently unfiltered collection."""
        from pydantic import ValidationError as PydanticValidationError

        # A variable, not a literal: the typo has to stay *data*, or the type
        # checker rejects it as an unknown keyword and the linter inlines it
        # straight back into one.
        mistyped = {"statuss": "active"}

        with pytest.raises(PydanticValidationError, match="statuss"):
            ThingFilter(**mistyped)

    def test_search_requires_a_minimum_length(self) -> None:
        """A one-character search is a full scan dressed as a filter."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError, match="at least"):
            SearchFilter(q="a")

    def test_time_range_bounds_are_optional(self) -> None:
        assert TimeRangeFilter().active_filters() == {}


class TestIndexAwareness:
    def test_unindexed_filters_are_reported(self) -> None:
        """A filter that scans by accident is how staging passes and prod times out."""
        assert unindexed_filters(SPECS) == ["name"]

    def test_all_indexed_reports_nothing(self) -> None:
        assert unindexed_filters([FilterSpec("status", _things.c.status)]) == []
