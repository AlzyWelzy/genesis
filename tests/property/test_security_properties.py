"""Invariants of the security primitives, over generated input.

These are the functions where an input-dependent failure is not an inconvenience
but a vulnerability: a LIKE pattern that escapes its own escaping is wildcard
injection, a signature that verifies under the wrong secret is no signature, and
a path helper that resolves outside its root is arbitrary file access.
"""

import contextlib
import re
from pathlib import Path

from hypothesis import assume, given
from hypothesis import strategies as st
from sqlalchemy import Column, Integer, MetaData, String, Table

from app.common.filtering import FilterSpec, Operator, _escape_like
from app.common.utils import crypto
from app.common.utils.files import safe_join

_metadata = MetaData()
_rows = Table(
    "rows", _metadata, Column("id", Integer, primary_key=True), Column("name", String)
)

TEXT = st.text(max_size=100)
SECRETS = st.text(min_size=1, max_size=64)


class TestLikeEscaping:
    """A search term is user input reaching a SQL pattern.

    Unescaped, ``%`` matches everything and ``_`` matches any character — so a
    user searching for "100%" silently receives the entire table, and a search
    for "a_b" matches "axb". Neither raises.
    """

    @given(TEXT)
    def test_every_wildcard_in_the_escaped_term_is_escaped(self, term: str) -> None:
        """Stated over the escaper itself rather than the compiled SQL.

        Parsing SQL back out of a compiled statement re-implements a quoting
        rule inside the test, which is how a test ends up asserting its own bug.
        """
        escaped = _escape_like(term)

        for match in re.finditer(r"[%_]", escaped):
            preceding = escaped[: match.start()]
            backslashes = len(preceding) - len(preceding.rstrip("\\"))
            assert backslashes % 2 == 1, f"unescaped wildcard in {escaped!r}"

    @given(TEXT)
    def test_escaping_preserves_every_literal_character(self, term: str) -> None:
        """Escaping must not lose, reorder or duplicate the user's characters."""
        escaped = _escape_like(term)
        unescaped = (
            escaped.replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\")
        )

        assert unescaped == term

    @given(TEXT)
    def test_building_a_filter_never_raises(self, term: str) -> None:
        """Every operator must survive arbitrary user text."""
        for operator in (
            Operator.CONTAINS,
            Operator.STARTS_WITH,
            Operator.EQ,
            Operator.NE,
        ):
            FilterSpec("name", _rows.c.name, operator).build(term)


class TestSigning:
    @given(st.binary(max_size=500), SECRETS)
    def test_a_signature_verifies_under_its_own_secret(
        self, payload: bytes, secret: str
    ) -> None:
        signature = crypto.sign_payload(payload, secret)
        assert crypto.verify_signature(payload, signature, secret)

    @given(st.binary(max_size=500), SECRETS, SECRETS)
    def test_a_signature_never_verifies_under_another_secret(
        self, payload: bytes, secret: str, other: str
    ) -> None:
        assume(secret != other)
        signature = crypto.sign_payload(payload, secret)

        assert not crypto.verify_signature(payload, signature, other)

    @given(st.binary(max_size=500), st.binary(max_size=500), SECRETS)
    def test_a_signature_does_not_transfer_between_payloads(
        self, payload: bytes, other: bytes, secret: str
    ) -> None:
        assume(payload != other)
        signature = crypto.sign_payload(payload, secret)

        assert not crypto.verify_signature(other, signature, secret)

    @given(st.text(max_size=100), st.binary(max_size=100), SECRETS)
    def test_verifying_arbitrary_input_returns_false_rather_than_raising(
        self, signature: str, payload: bytes, secret: str
    ) -> None:
        """A signature is attacker-supplied; a crash there is a denial of service."""
        crypto.verify_signature(payload, signature, secret)


class TestTokenGeneration:
    @given(st.integers(min_value=1, max_value=128))
    def test_tokens_are_url_safe(self, length: int) -> None:
        """A token lands in URLs and headers."""
        assert re.fullmatch(r"[A-Za-z0-9_-]+", crypto.generate_token(length))

    @given(st.integers(min_value=16, max_value=64))
    def test_tokens_do_not_repeat(self, length: int) -> None:
        """Tokens must not collide at realistic entropy.

        ``length`` is *bytes*, so a caller passing 1 gets a two-character token
        that collides readily — a caller error the docstring already warns
        about, not a defect in the generator.
        """
        assert len({crypto.generate_token(length) for _ in range(50)}) == 50

    @given(st.integers(min_value=1, max_value=12))
    def test_numeric_codes_have_exactly_the_requested_digits(self, digits: int) -> None:
        """A code short by a digit reveals that leading zeros were dropped."""
        code = crypto.generate_numeric_code(digits)

        assert len(code) == digits
        assert code.isdigit()

    @given(TEXT, TEXT)
    def test_constant_time_compare_agrees_with_equality(
        self, left: str, right: str
    ) -> None:
        assert crypto.constant_time_compare(left, right) == (left == right)


class TestSafeJoin:
    ROOT = Path("/srv/storage")

    @given(st.text(max_size=120))
    def test_the_result_never_escapes_the_root(self, key: str) -> None:
        """A storage key is attacker-supplied in every upload flow."""
        try:
            resolved = safe_join(self.ROOT, key)
        except ValueError:
            return
        assert resolved.is_relative_to(self.ROOT.resolve())

    @given(st.text(max_size=120))
    def test_the_result_is_never_the_root_itself(self, key: str) -> None:
        """A key naming the root makes the caller write to a directory."""
        try:
            resolved = safe_join(self.ROOT, key)
        except ValueError:
            return
        assert resolved != self.ROOT.resolve()

    @given(st.text(max_size=120))
    def test_only_value_error_is_raised(self, key: str) -> None:
        """The documented failure mode; anything else becomes a 500."""
        with contextlib.suppress(ValueError):
            safe_join(self.ROOT, key)
