"""Tests for schema safety and the enum persistence policy."""

import json

import pytest
from pydantic import SecretStr

from app.common.constants import REDACTED
from app.common.enums import JobStatus, SortOrder, enum_column
from app.common.responses import (
    DataResponse,
    ErrorResponse,
    ListResponse,
    error_responses,
)
from app.common.types import BaseSchema, SafeResponseSchema, assert_no_secrets


class TestSafeResponseSchema:
    def test_secrets_are_masked_in_json(self) -> None:
        """model_dump_json is the path an API uses, and the one that leaks."""

        class Response(SafeResponseSchema):
            name: str
            api_key: SecretStr

        payload = Response(name="x", api_key=SecretStr("sk-live-abc")).model_dump_json()
        assert "sk-live-abc" not in payload
        assert REDACTED in payload

    def test_ordinary_fields_pass_through(self) -> None:
        class Response(SafeResponseSchema):
            name: str
            count: int

        body = json.loads(Response(name="x", count=3).model_dump_json())
        assert body == {"name": "x", "count": 3}

    def test_assert_no_secrets_catches_a_leak(self) -> None:
        with pytest.raises(RuntimeError, match="credential"):
            assert_no_secrets({"api_key": "sk-live-abc"})

    def test_assert_no_secrets_allows_a_redacted_value(self) -> None:
        assert_no_secrets({"api_key": REDACTED})

    def test_assert_no_secrets_allows_none(self) -> None:
        assert_no_secrets({"api_key": None})


class TestBaseSchema:
    def test_unknown_fields_are_rejected(self) -> None:
        """A client typo must be a 422, not a silently dropped field."""
        from pydantic import ValidationError as PydanticValidationError

        class Body(BaseSchema):
            name: str

        # A variable, not a literal: the typo has to stay *data*, or the type
        # checker rejects it as an unknown keyword and the linter inlines it
        # straight back into one.
        mistyped = {"name": "x", "emial": "typo@example.com"}

        with pytest.raises(PydanticValidationError, match="emial"):
            Body(**mistyped)

    def test_strings_are_stripped(self) -> None:
        class Body(BaseSchema):
            name: str

        assert Body(name="  padded  ").name == "padded"


class TestEnumColumn:
    def test_uses_varchar_with_a_check_constraint(self) -> None:
        """Native ENUM makes adding a value an ALTER TYPE that cannot roll back."""
        column = enum_column(JobStatus)
        assert column.native_enum is False
        assert column.create_constraint is True

    def test_the_constraint_is_deterministically_named(self) -> None:
        """An unnamed constraint cannot be dropped by a generated migration."""
        assert enum_column(JobStatus).name == "ck_jobstatus"

    def test_stores_the_value_not_the_member_name(self) -> None:
        """The value is the public contract; the member name stays renameable."""
        column = enum_column(SortOrder)
        assert sorted(column.enums) == ["asc", "desc"]

    def test_a_too_narrow_column_is_rejected(self) -> None:
        """Silent truncation of an enum value would corrupt the column."""
        with pytest.raises(ValueError, match="widen the column"):
            enum_column(JobStatus, length=3)

    def test_a_wide_enough_column_is_accepted(self) -> None:
        assert enum_column(JobStatus, length=16).length == 16


class TestResponseEnvelopes:
    def test_data_response_wraps(self) -> None:
        assert DataResponse.of({"a": 1}).model_dump() == {"data": {"a": 1}}

    def test_list_response_counts(self) -> None:
        body = ListResponse.of([1, 2, 3]).model_dump()
        assert body == {"data": [1, 2, 3], "count": 3}

    def test_error_responses_builds_a_documented_subset(self) -> None:
        mapping = error_responses(409, 422)
        assert sorted(mapping) == [409, 422]
        assert mapping[409]["model"] is ErrorResponse

    def test_an_undocumented_status_is_rejected(self) -> None:
        """Better than silently documenting nothing for that status."""
        with pytest.raises(KeyError):
            error_responses(418)
