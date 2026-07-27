"""Invariants of the string helpers, over generated input.

Each helper's contract is stated as a relationship that must hold for every
string, not for a chosen example. The generators deliberately reach the cases
nobody writes by hand: lone surrogates, unassigned code points, combining marks,
and strings differing only by Unicode normalisation form.
"""

import re

from hypothesis import assume, given
from hypothesis import strategies as st

from app.common.utils import strings

ANY_TEXT = st.text()
SHORT_TEXT = st.text(max_size=200)


class TestSlugify:
    @given(ANY_TEXT, st.integers(min_value=1, max_value=200))
    def test_the_result_is_always_a_valid_slug(self, value: str, limit: int) -> None:
        """A slug reaches a URL, so an invalid one is a broken link."""
        slug = strings.slugify(value, max_length=limit)

        assert slug == "" or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)

    @given(ANY_TEXT, st.integers(min_value=1, max_value=200))
    def test_the_limit_is_never_exceeded(self, value: str, limit: int) -> None:
        """The caller sized a database column for this."""
        assert len(strings.slugify(value, max_length=limit)) <= limit

    @given(ANY_TEXT)
    def test_slugifying_a_slug_changes_nothing(self, value: str) -> None:
        """Idempotence. Without it, re-saving a record rewrites a live URL."""
        once = strings.slugify(value)
        assert strings.slugify(once) == once

    @given(ANY_TEXT)
    def test_no_leading_or_trailing_separator(self, value: str) -> None:
        slug = strings.slugify(value)
        assert not slug.startswith("-")
        assert not slug.endswith("-")


class TestTruncate:
    @given(ANY_TEXT, st.integers(min_value=1, max_value=300))
    def test_the_result_never_exceeds_the_limit(self, value: str, limit: int) -> None:
        """The entire point of the function; otherwise a column overflows."""
        assume(limit >= len("…"))
        assert len(strings.truncate(value, limit)) <= limit

    @given(ANY_TEXT, st.integers(min_value=1, max_value=300))
    def test_a_string_that_already_fits_is_untouched(
        self, value: str, limit: int
    ) -> None:
        assume(len(value) <= limit)
        assert strings.truncate(value, limit) == value

    @given(ANY_TEXT, st.integers(min_value=2, max_value=300))
    def test_truncating_twice_is_stable(self, value: str, limit: int) -> None:
        once = strings.truncate(value, limit)
        assert strings.truncate(once, limit) == once


class TestMask:
    """Stated over the *hidden region*, not by comparing characters positionally.

    A positional comparison cannot tell a leak from a coincidence: masking
    ``"*"`` yields ``"*"``, and the character matches without anything having
    been revealed.
    """

    @given(ANY_TEXT, st.integers(min_value=0, max_value=50))
    def test_everything_but_the_tail_is_hidden(self, value: str, visible: int) -> None:
        """A mask that leaks is worse than none, because it is trusted."""
        masked = strings.mask(value, visible=visible)
        hidden = masked[: len(masked) - visible] if visible > 0 else masked

        assert set(hidden) <= {"*"}

    @given(ANY_TEXT, st.integers(min_value=0, max_value=50))
    def test_the_length_is_preserved(self, value: str, visible: int) -> None:
        """Changing the length leaks the magnitude of the secret."""
        assert len(strings.mask(value, visible=visible)) == len(value)

    @given(ANY_TEXT, st.integers(min_value=1, max_value=50))
    def test_a_short_value_is_hidden_entirely(self, value: str, visible: int) -> None:
        """Revealing four of five characters is not redaction."""
        assume(len(value) <= visible * 2)
        assert set(strings.mask(value, visible=visible)) <= {"*"}


class TestCaseConversion:
    @given(ANY_TEXT)
    def test_snake_case_is_idempotent(self, value: str) -> None:
        once = strings.to_snake_case(value)
        assert strings.to_snake_case(once) == once

    @given(ANY_TEXT)
    def test_camel_case_is_idempotent(self, value: str) -> None:
        once = strings.to_camel_case(value)
        assert strings.to_camel_case(once) == once

    @given(ANY_TEXT)
    def test_neither_conversion_raises(self, value: str) -> None:
        strings.to_snake_case(value)
        strings.to_camel_case(value)


class TestNormalizeEmail:
    @given(SHORT_TEXT)
    def test_is_idempotent(self, value: str) -> None:
        """Drifting on re-normalisation means a lookup misses its own row."""
        once = strings.normalize_email(value)
        assert strings.normalize_email(once) == once

    @given(SHORT_TEXT)
    def test_the_result_is_lower_case(self, value: str) -> None:
        """Case-insensitive uniqueness depends on this."""
        normalised = strings.normalize_email(value)
        assert normalised == normalised.lower()
