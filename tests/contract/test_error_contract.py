"""The error envelope and its codes, locked against silent change.

A client branches on ``error.code`` and reads ``error.message``. Both are load
bearing for somebody else's software, which is what makes them different from
the rest of this codebase: they cannot be improved unilaterally.
"""

import ast
import pathlib

import pytest

from app.core import exceptions as exc_module
from app.core.exceptions import AppError, build_error_body

#: Every error code this API is allowed to emit.
#:
#: Written out rather than derived from the classes. A derived set passes
#: whatever the code says, which would defeat the entire purpose — the test
#: exists precisely to notice when the code changes.
PUBLISHED_ERROR_CODES = frozenset(
    {
        # Domain errors, raised by services.
        "validation_error",
        "business_rule_violation",
        "not_found",
        "conflict",
        "unauthenticated",
        "permission_denied",
        "rate_limited",
        "service_unavailable",
        "external_service_error",
        "internal_error",
        # Raised by platform helpers with an explicit code.
        "invalid_cursor",
        "invalid_filter",
        "invalid_sort_field",
        "password_policy_violation",
        "password_breached",
        # Emitted only by the global handlers, with no class of their own.
        "stale_data",
        "already_exists",
        "method_not_allowed",
        "http_error",
    }
)

#: The keys inside ``error``. A client destructuring this breaks if one moves.
ENVELOPE_FIELDS = frozenset({"code", "message", "details", "request_id"})


def _app_error_subclasses(cls: type[AppError] = AppError) -> set[type[AppError]]:
    """Every ``AppError`` class, including the base, however deeply nested."""
    found: set[type[AppError]] = {cls}
    for subclass in cls.__subclasses__():
        found |= _app_error_subclasses(subclass)
    return found


def _codes_in_source() -> set[str]:
    """Every error code the application can emit, read from the source.

    Two mechanisms produce a code, and a check that sees only one is a check
    that misses half the contract:

    * a class attribute — ``code = "not_found"``;
    * a keyword at a raise site — ``ValidationError(..., code="invalid_cursor")``.

    Five codes exist only in the second form, so a scan of the class hierarchy
    reports them as deleted the moment it is written. Parsing the source finds
    both, which is also what makes this robust against a code being introduced
    somewhere nobody expected.
    """
    codes: set[str] = set()
    for path in pathlib.Path("app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                codes |= {
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                }
    return codes | {cls.code for cls in _app_error_subclasses()}


class TestErrorCodes:
    def test_no_code_is_emitted_that_clients_do_not_know_about(self) -> None:
        """A new code is safe to add — but only deliberately.

        Failing here means a code was introduced without being written into the
        manifest. Add it in the same commit; that is the whole ceremony.
        """
        unknown = _codes_in_source() - PUBLISHED_ERROR_CODES

        assert unknown == set(), f"undocumented error codes: {sorted(unknown)}"

    def test_no_published_code_has_disappeared(self) -> None:
        """Renaming or removing a code is a **breaking change**.

        If this fails, do not simply edit the manifest. Either keep the old code
        alongside the new one, or version the API. A client cannot be patched by
        editing this repository.
        """
        emitted = _codes_in_source() | {
            # Emitted positionally by the global handlers, so neither a class
            # attribute nor a `code=` keyword.
            "stale_data",
            "already_exists",
            "method_not_allowed",
            "http_error",
        }
        missing = PUBLISHED_ERROR_CODES - emitted

        assert missing == set(), (
            f"error codes no longer emitted: {sorted(missing)} — removing one is "
            "a breaking change for every client branching on it"
        )

    def test_codes_are_stable_identifiers_not_prose(self) -> None:
        """Lowercase snake_case, because clients compare them literally."""
        for code in PUBLISHED_ERROR_CODES:
            assert code.islower()
            assert " " not in code
            assert code.replace("_", "").isalnum()

    def test_every_code_is_unique_to_one_meaning(self) -> None:
        """Two classes sharing a code make the code useless to branch on."""
        codes = [cls.code for cls in _app_error_subclasses()]

        assert len(codes) == len(set(codes)), "two error classes share a code"


class TestEnvelopeShape:
    def test_the_envelope_is_nested_under_error(self) -> None:
        """Clients destructure ``response["error"]["code"]``."""
        body = build_error_body("not_found", "Resource not found.")

        assert set(body) == {"error"}
        assert "code" in body["error"]

    def test_no_unexpected_field_appears_inside_error(self) -> None:
        """A strict client may reject an unknown key outright."""
        body = build_error_body("not_found", "Resource not found.")

        assert set(body["error"]) <= ENVELOPE_FIELDS

    def test_code_and_message_are_always_present(self) -> None:
        """The two fields every client reads."""
        body = build_error_body("not_found", "Resource not found.")

        assert body["error"]["code"] == "not_found"
        assert body["error"]["message"]


class TestStatusCodeMapping:
    @pytest.mark.parametrize(
        ("code", "status"),
        [
            ("validation_error", 422),
            ("business_rule_violation", 409),
            ("not_found", 404),
            ("conflict", 409),
            ("unauthenticated", 401),
            ("permission_denied", 403),
            ("rate_limited", 429),
            ("service_unavailable", 503),
            ("internal_error", 500),
        ],
    )
    def test_a_code_keeps_its_status(self, code: str, status: int) -> None:
        """Clients retry on some statuses and not others.

        Changing the status a code maps to changes retry behaviour in software
        this repository cannot patch — a 409 that becomes a 500 turns a
        "refetch and retry" into an alert.
        """
        matching = [cls for cls in _app_error_subclasses() if cls.code == code]
        assert matching, f"no error class emits {code}"

        assert matching[0].status_code == status

    def test_unauthenticated_always_carries_a_challenge(self) -> None:
        """A 401 without ``WWW-Authenticate`` is not a valid HTTP challenge."""
        error = exc_module.AuthenticationError()

        assert error.headers.get("WWW-Authenticate") == "Bearer"

    def test_rate_limited_always_carries_retry_after(self) -> None:
        """Without it, a client's only strategy is to retry blindly."""
        error = exc_module.RateLimitError()

        assert "Retry-After" in error.headers
