"""Every production-unsafe setting must refuse to boot, and safe ones must not.

Each case is stated as "this one setting, otherwise valid", so a failure names
the exact check that regressed rather than reporting that some configuration
somewhere is wrong.
"""

import pytest
from pydantic import ValidationError

from app.core.settings import Settings

#: A configuration that is production-safe in every respect. Each test below
#: makes exactly one thing wrong, so nothing passes for an unrelated reason.
SAFE_PRODUCTION = {
    "APP__ENVIRONMENT": "production",
    "APP__DEBUG": "false",
    "APP__TRUSTED_HOSTS": '["api.example.com"]',
    "APP__CORS_ORIGINS": '["https://app.example.com"]',
    "LOGGING__JSON_FORMAT": "true",
    "LOGGING__LOG_REQUEST_BODY": "false",
    "RATE_LIMIT__ENABLED": "true",
    "DATABASE__ECHO": "false",
    "DATABASE__URL": "postgresql+asyncpg://user:pass@db:5432/genesis",
    "STORAGE__PROVIDER": "s3",
    "STORAGE__BUCKET": "genesis-uploads",
    "EMAIL__PROVIDER": "smtp",
    "EMAIL__SMTP_HOST": "smtp.example.com",
}


@pytest.fixture
def production_env(monkeypatch):
    """Apply a fully safe production environment, isolated from the real one."""

    def apply(**overrides: str) -> None:
        for key in list(SAFE_PRODUCTION) + list(overrides):
            monkeypatch.delenv(key, raising=False)
        for key, value in (SAFE_PRODUCTION | overrides).items():
            monkeypatch.setenv(key, value)

    return apply


class TestSafeConfigurationBoots:
    def test_a_correct_production_configuration_is_accepted(
        self, production_env
    ) -> None:
        """A safe configuration must still boot.

        A validator that rejects everything is as useless as one that rejects
        nothing, and far easier to write by accident.
        """
        production_env()

        assert Settings().app.is_production

    def test_non_production_environments_are_not_policed(self, production_env) -> None:
        """Local development stays convenient; these rules are about deploys."""
        production_env(
            APP__ENVIRONMENT="local",
            APP__DEBUG="true",
            STORAGE__PROVIDER="local",
            EMAIL__PROVIDER="console",
        )

        assert Settings().app.debug is True


class TestUnsafeConfigurationIsRefused:
    @pytest.mark.parametrize(
        ("label", "override", "expected"),
        [
            ("debug tracebacks", {"APP__DEBUG": "true"}, "APP__DEBUG"),
            ("wildcard CORS", {"APP__CORS_ORIGINS": '["*"]'}, "APP__CORS_ORIGINS"),
            ("no trusted hosts", {"APP__TRUSTED_HOSTS": "[]"}, "APP__TRUSTED_HOSTS"),
            (
                "unparseable logs",
                {"LOGGING__JSON_FORMAT": "false"},
                "LOGGING__JSON_FORMAT",
            ),
            (
                "request bodies in logs",
                {"LOGGING__LOG_REQUEST_BODY": "true"},
                "LOGGING__LOG_REQUEST_BODY",
            ),
            (
                "unthrottled API",
                {"RATE_LIMIT__ENABLED": "false"},
                "RATE_LIMIT__ENABLED",
            ),
            ("SQL echo", {"DATABASE__ECHO": "true"}, "DATABASE__ECHO"),
            ("ephemeral storage", {"STORAGE__PROVIDER": "local"}, "STORAGE__PROVIDER"),
            ("email to a log file", {"EMAIL__PROVIDER": "console"}, "EMAIL__PROVIDER"),
        ],
    )
    def test_one_unsafe_setting_prevents_startup(
        self, production_env, label: str, override: dict, expected: str
    ) -> None:
        """Crashing at boot is the point: a failed deploy beats a silent breach."""
        production_env(**override)

        with pytest.raises(ValidationError) as exc:
            Settings()

        assert expected in str(exc.value), f"{label} was not reported"

    def test_every_problem_is_reported_at_once(self, production_env) -> None:
        """Fixing configuration one crash at a time is a long afternoon."""
        production_env(
            APP__DEBUG="true",
            RATE_LIMIT__ENABLED="false",
            STORAGE__PROVIDER="local",
        )

        with pytest.raises(ValidationError) as exc:
            Settings()

        message = str(exc.value)
        assert "APP__DEBUG" in message
        assert "RATE_LIMIT__ENABLED" in message
        assert "STORAGE__PROVIDER" in message

    def test_the_message_says_what_would_happen(self, production_env) -> None:
        """A rule without its reason gets overridden by whoever is on call."""
        production_env(STORAGE__PROVIDER="local")

        with pytest.raises(ValidationError) as exc:
            Settings()

        assert "lost on restart" in str(exc.value)


class TestSilentFailureProviders:
    """The two providers whose production failure mode is *silence*.

    Neither raises, fails a request, or moves a metric. A `local` storage
    provider writes to container-local disk, so uploads are invisible to other
    replicas and vanish on restart. A `console` email provider logs the message
    instead of sending it, so no password reset arrives. The first signal in both
    cases is a customer asking where something went.
    """

    def test_local_storage_is_refused(self, production_env) -> None:
        production_env(STORAGE__PROVIDER="local")

        with pytest.raises(ValidationError, match="STORAGE__PROVIDER"):
            Settings()

    def test_console_email_is_refused(self, production_env) -> None:
        production_env(EMAIL__PROVIDER="console")

        with pytest.raises(ValidationError, match="EMAIL__PROVIDER"):
            Settings()

    def test_the_real_providers_are_accepted(self, production_env) -> None:
        production_env(STORAGE__PROVIDER="s3", EMAIL__PROVIDER="smtp")
        settings = Settings()

        assert settings.storage.provider == "s3"
        assert settings.email.provider == "smtp"
