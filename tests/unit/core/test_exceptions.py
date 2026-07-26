"""Tests for the error hierarchy and response envelope.

The envelope is a public API contract: clients branch on ``error.code``. These
tests pin the shape and the status mapping so a refactor cannot silently change
what integrators receive.
"""

import pytest

from app.core.context import request_id_var
from app.core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
    build_error_body,
)


class TestStatusMapping:
    """Each error type maps to exactly one status and one stable code."""

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (ValidationError(), 422, "validation_error"),
            (BusinessRuleError(), 409, "business_rule_violation"),
            (NotFoundError(), 404, "not_found"),
            (ConflictError(), 409, "conflict"),
            (AuthenticationError(), 401, "unauthenticated"),
            (AuthorizationError(), 403, "permission_denied"),
            (RateLimitError(), 429, "rate_limited"),
            (ServiceUnavailableError(), 503, "service_unavailable"),
            (ExternalServiceError(), 502, "external_service_error"),
        ],
    )
    def test_error_maps_to_status_and_code(
        self, error: AppError, status: int, code: str
    ) -> None:
        assert error.status_code == status
        assert error.code == code

    def test_authentication_error_sets_challenge_header(self) -> None:
        """A 401 without WWW-Authenticate is not a valid HTTP challenge."""
        assert AuthenticationError().headers["WWW-Authenticate"] == "Bearer"

    def test_rate_limit_error_sets_retry_after(self) -> None:
        """Without Retry-After a client can only retry blindly."""
        assert RateLimitError(retry_after=30).headers["Retry-After"] == "30"


class TestErrorEnvelope:
    """The single response shape every failure produces."""

    def test_minimal_envelope(self) -> None:
        request_id_var.set(None)
        assert build_error_body("not_found", "Resource not found.") == {
            "error": {"code": "not_found", "message": "Resource not found."}
        }

    def test_details_are_included_when_present(self) -> None:
        request_id_var.set(None)
        body = build_error_body("validation_error", "Bad.", {"fields": ["email"]})
        assert body["error"]["details"] == {"fields": ["email"]}

    def test_request_id_is_attached_from_context(self) -> None:
        """The ID is what lets support find the log line for a user's report."""
        request_id_var.set("req-abc-123")
        try:
            body = build_error_body("internal_error", "Oops.")
            assert body["error"]["request_id"] == "req-abc-123"
        finally:
            request_id_var.set(None)


class TestErrorConstruction:
    """Overrides available at the raise site."""

    def test_message_and_code_can_be_overridden(self) -> None:
        error = NotFoundError("No such invoice.", code="invoice_not_found")
        assert error.message == "No such invoice."
        assert error.code == "invoice_not_found"
        assert error.status_code == 404

    def test_subclass_defaults_are_inherited(self) -> None:
        """A feature error subclasses a base and inherits its status."""

        class InvoiceNotFoundError(NotFoundError):
            code = "invoice_not_found"
            message = "Invoice not found."

        error = InvoiceNotFoundError()
        assert error.status_code == 404
        assert error.code == "invoice_not_found"

    def test_app_error_is_an_exception(self) -> None:
        """Services must be able to raise these like any other exception."""
        with pytest.raises(AppError, match=r"Resource not found\."):
            raise NotFoundError
