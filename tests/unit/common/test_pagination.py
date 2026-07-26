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


class TestCursorEncoding:
    SECRET = "test-signing-secret"

    def test_round_trip(self) -> None:
        from app.common.pagination import decode_cursor, encode_cursor

        values = {"created_at": "2026-01-15T10:30:00+00:00", "id": "0193f4a2"}
        cursor = encode_cursor(values, secret=self.SECRET)
        assert decode_cursor(cursor, secret=self.SECRET) == values

    def test_cursor_is_opaque(self) -> None:
        """A client that can read a cursor will come to depend on its shape."""
        from app.common.pagination import encode_cursor

        cursor = encode_cursor({"id": "secret-value"}, secret=self.SECRET)
        assert "secret-value" not in cursor
        assert "created_at" not in cursor

    def test_url_safe(self) -> None:
        from app.common.pagination import encode_cursor

        cursor = encode_cursor({"id": "a" * 50}, secret=self.SECRET)
        assert "+" not in cursor
        assert "/" not in cursor
        assert "=" not in cursor

    def test_tampered_cursor_is_rejected(self) -> None:
        import base64
        import json

        from app.common.pagination import decode_cursor
        from app.core.exceptions import ValidationError

        forged = (
            base64.urlsafe_b64encode(
                json.dumps({"v": 1, "k": {"id": "anything"}}).encode() + b".fakesig"
            )
            .decode()
            .rstrip("=")
        )
        with pytest.raises(ValidationError):
            decode_cursor(forged, secret=self.SECRET)

    def test_wrong_secret_is_rejected(self) -> None:
        from app.common.pagination import decode_cursor, encode_cursor
        from app.core.exceptions import ValidationError

        cursor = encode_cursor({"id": "1"}, secret=self.SECRET)
        with pytest.raises(ValidationError):
            decode_cursor(cursor, secret="different-secret")

    def test_garbage_is_rejected(self) -> None:
        from app.common.pagination import decode_cursor
        from app.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            decode_cursor("!!!not-base64!!!", secret=self.SECRET)

    def test_stale_version_is_rejected(self) -> None:
        """A cursor minted before a sort change must fail, not page wrongly."""
        import base64
        import hashlib
        import hmac
        import json

        from app.common.pagination import decode_cursor
        from app.core.exceptions import ValidationError

        payload = json.dumps(
            {"v": 999, "k": {"id": "1"}}, separators=(",", ":"), sort_keys=True
        ).encode()
        signature = hmac.new(self.SECRET.encode(), payload, hashlib.sha256).digest()[
            :16
        ]
        cursor = (
            base64.urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")
        )
        with pytest.raises(ValidationError):
            decode_cursor(cursor, secret=self.SECRET)

    def test_error_does_not_reveal_the_failure_mode(self) -> None:
        """Distinguishing bad-signature from bad-base64 lets a client probe."""
        from app.common.pagination import decode_cursor
        from app.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as garbage:
            decode_cursor("!!!", secret=self.SECRET)
        with pytest.raises(ValidationError) as wrong_secret:
            decode_cursor(
                __import__(
                    "app.common.pagination", fromlist=["encode_cursor"]
                ).encode_cursor({"id": "1"}, secret="other"),
                secret=self.SECRET,
            )
        assert garbage.value.code == wrong_secret.value.code
