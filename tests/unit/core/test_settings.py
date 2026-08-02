"""Tests for configuration validation.

The production-safety validator is the subject here. Each case corresponds to a
real class of incident, and the assertion is always that the application
*refuses to start* — a misconfigured instance that crashes is a failed deploy,
while one that starts is an exposure.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.settings import Settings

_SAFE_PRODUCTION: dict[str, dict[str, object]] = {
    "app": {
        "environment": "production",
        "debug": False,
        "cors_origins": ["https://app.example.com"],
        "trusted_hosts": ["api.example.com"],
    },
    "database": {"url": "postgresql+asyncpg://u:p@db:5432/genesis", "echo": False},
    "logging": {"json_format": True, "log_request_body": False},
    "rate_limit": {"enabled": True},
    # The real providers. Omitting these leaves the *development* defaults —
    # `local` storage and `console` email — which the validator now refuses,
    # correctly: local storage loses every upload on restart and console email
    # sends nothing at all, both silently.
    "storage": {"provider": "s3", "bucket": "genesis-uploads"},
    "email": {"provider": "smtp", "smtp_host": "smtp.example.com"},
}


def _production_settings(**overrides: dict[str, object]) -> Settings:
    """Build production settings, merging per-section overrides."""
    config = {
        section: {**values, **overrides.get(section, {})}
        for section, values in _SAFE_PRODUCTION.items()
    }
    return Settings.model_validate(config)


class TestProductionSafety:
    """The validator that refuses unsafe production configurations."""

    def test_safe_configuration_is_accepted(self) -> None:
        assert _production_settings().app.is_production is True

    @pytest.mark.parametrize(
        ("section", "override", "expected"),
        [
            ("app", {"debug": True}, "APP__DEBUG"),
            ("app", {"cors_origins": ["*"]}, "APP__CORS_ORIGINS"),
            ("app", {"trusted_hosts": []}, "APP__TRUSTED_HOSTS"),
            ("logging", {"json_format": False}, "LOGGING__JSON_FORMAT"),
            ("logging", {"log_request_body": True}, "LOGGING__LOG_REQUEST_BODY"),
            ("rate_limit", {"enabled": False}, "RATE_LIMIT__ENABLED"),
            ("database", {"echo": True}, "DATABASE__ECHO"),
        ],
    )
    def test_unsafe_setting_is_rejected(
        self, section: str, override: dict[str, object], expected: str
    ) -> None:
        with pytest.raises(PydanticValidationError) as exc_info:
            _production_settings(**{section: override})
        assert expected in str(exc_info.value)

    def test_all_problems_are_reported_together(self) -> None:
        """Reporting one failure at a time turns a deploy into a guessing game."""
        with pytest.raises(PydanticValidationError) as exc_info:
            _production_settings(
                app={"debug": True, "trusted_hosts": []}, rate_limit={"enabled": False}
            )
        message = str(exc_info.value)
        assert "APP__DEBUG" in message
        assert "APP__TRUSTED_HOSTS" in message
        assert "RATE_LIMIT__ENABLED" in message

    def test_local_environment_is_not_constrained(self) -> None:
        """Development must stay convenient; the rules apply to production only."""
        settings = Settings.model_validate(
            {
                "app": {
                    "environment": "local",
                    "debug": True,
                    "cors_origins": ["*"],
                },
                "database": {"url": "postgresql+asyncpg://u:p@localhost:5432/genesis"},
            }
        )
        assert settings.app.debug is True


class TestDerivedValues:
    """Properties other modules depend on."""

    def test_version_prefix_is_composed_from_parts(self) -> None:
        settings = Settings.model_validate(
            {
                "app": {"api_prefix": "/api", "api_version": "v2"},
                "database": {"url": "postgresql+asyncpg://u:p@localhost:5432/genesis"},
            }
        )
        assert settings.app.version_prefix == "/api/v2"

    def test_docs_can_be_disabled_independently_of_debug(self) -> None:
        settings = Settings.model_validate(
            {
                "app": {"enable_docs": False},
                "database": {"url": "postgresql+asyncpg://u:p@localhost:5432/genesis"},
            }
        )
        assert settings.app.docs_url is None
        assert settings.app.openapi_url is None
