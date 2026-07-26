"""Typed application configuration.

Why this file exists
--------------------
Configuration is the most common source of environment drift in a large SaaS
platform: a value read from ``os.environ`` deep inside a service is invisible,
untyped and impossible to validate at boot. This module declares the *entire*
configuration surface as nested Pydantic models so that:

* every setting has a type, a default (or is explicitly required) and a docstring;
* an invalid or missing value fails loudly at startup, never at 3am in a request;
* subsystems own their own settings block instead of sharing a flat namespace;
* tests construct a ``Settings`` instance directly without touching the env.

This module only *declares* the shape. Instantiation happens exactly once in
:mod:`app.core.config` — import ``settings`` from there, never from here.

Where values come from
----------------------
Sources, in decreasing priority:

1. Process environment — what a container orchestrator injects.
2. ``.env.<environment>`` — environment-specific overrides, if present.
3. ``.env`` — the developer's local file.
4. ``secrets_dir`` — one file per secret, the convention used by Docker secrets
   and Kubernetes secret volumes.

That last source is the reason this application needs no code change to move
off ``.env`` files: point ``SECRETS_DIR`` at the mounted volume and the same
fields are populated from files instead. See
``docs/architecture/configuration.md``.

Nested models are populated with a double-underscore delimiter::

    APP__NAME=Genesis
    DATABASE__URL=postgresql+asyncpg://user:pass@localhost/genesis
    JWT__ACCESS_TOKEN_EXPIRE_MINUTES=15
"""

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type Environment = Literal["local", "test", "development", "staging", "production"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: Environment name, read before validation so the matching ``.env.<name>``
#: overlay can be selected. This is the one place ``os.environ`` is read
#: directly, and only to decide which files to load.
_ENVIRONMENT: str = os.environ.get("APP__ENVIRONMENT", "local")

#: Directory of file-per-secret mounts. Absent locally, present in deployment.
_SECRETS_DIR: str | None = os.environ.get("SECRETS_DIR")


class AppSettings(BaseModel):
    """Identity and runtime behaviour of the ASGI application itself.

    Nothing here talks to an external system; these values decide how the app
    presents itself (title, docs, version) and which safety rails are on.
    """

    name: str = "Genesis"
    version: str = "0.1.0"
    description: str = "Genesis API"
    environment: Environment = "local"

    #: Root path for every API route. Version segments are added by the routers.
    api_prefix: str = "/api"

    #: Default API version mounted under the prefix, e.g. ``/api/v1``.
    api_version: str = "v1"

    #: Enables verbose errors. Must be False in production — a debug traceback
    #: in an HTTP response leaks source, paths and often credentials.
    debug: bool = False

    #: Serve the interactive docs. Independent of ``debug`` so a staging
    #: environment can expose docs without exposing tracebacks.
    enable_docs: bool = True

    #: Origins allowed by the CORS middleware. Never ``["*"]`` with credentials:
    #: the combination is rejected by browsers and, where honoured, permits any
    #: site to make authenticated requests on a user's behalf.
    cors_origins: list[str] = Field(default_factory=list)

    #: Host headers accepted in production. Empty disables the check. Guards
    #: against Host-header poisoning of generated links and cache keys.
    trusted_hosts: list[str] = Field(default_factory=list)

    #: Set when the app runs behind a proxy that strips a path prefix.
    root_path: str = ""

    #: Compress responses above this size. Below ~500 bytes compression costs
    #: more CPU than it saves bandwidth.
    gzip_minimum_size: int = 1000

    @property
    def is_production(self) -> bool:
        """Whether the app runs in the production environment."""
        return self.environment == "production"

    @property
    def is_testing(self) -> bool:
        """Whether the app runs under the test suite."""
        return self.environment == "test"

    @property
    def version_prefix(self) -> str:
        """Full prefix for versioned routes, e.g. ``/api/v1``."""
        return f"{self.api_prefix}/{self.api_version}"

    @property
    def docs_url(self) -> str | None:
        """OpenAPI docs path, or ``None`` when docs are disabled."""
        return "/docs" if self.enable_docs else None

    @property
    def openapi_url(self) -> str | None:
        """OpenAPI schema path, or ``None`` when docs are disabled."""
        return "/openapi.json" if self.enable_docs else None


class DatabaseSettings(BaseModel):
    """PostgreSQL connection and async engine pool tuning.

    Owned by :mod:`app.infrastructure.database.session`, the only module that
    reads these values.
    """

    #: Must use the asyncpg driver: ``postgresql+asyncpg://...``
    url: PostgresDsn

    #: Emit every statement to the logger. Development only — extremely noisy.
    echo: bool = False

    #: Connections kept open per worker process. The real ceiling is
    #: ``pool_size * workers * replicas`` against PostgreSQL's ``max_connections``;
    #: exceeding it produces connection refusals under load, not slowness.
    pool_size: int = 10

    #: Extra connections allowed above ``pool_size`` during bursts.
    max_overflow: int = 10

    #: Seconds to wait for a free connection before raising.
    pool_timeout: int = 30

    #: Recycle connections older than this (seconds) to survive proxy timeouts.
    pool_recycle: int = 1800

    #: Ping a connection before handing it out. Costs a round trip; prevents
    #: handing out sockets a load balancer has already killed.
    pool_pre_ping: bool = True

    #: Server-side statement timeout (ms). A backstop against one pathological
    #: query holding a connection and cascading into pool exhaustion.
    statement_timeout_ms: int = 30_000

    #: Optional read-replica URL. When set, read-only sessions are routed here.
    read_replica_url: PostgresDsn | None = None


class JWTSettings(BaseModel):
    """Token signing material, lifetimes and rotation.

    Asymmetric (EdDSA/Ed25519) signing is the default so services that only
    *verify* tokens never need the private key. Consumed exclusively by
    :mod:`app.core.security`.
    """

    #: EdDSA (Ed25519) by default. RS256/ES256 supported for legacy interop.
    algorithm: Literal["EdDSA", "RS256", "ES256"] = "EdDSA"

    #: PEM signing key. Never commit; never ship to verify-only services.
    private_key_path: Path = Path("keys/private.pem")

    #: PEM verification key. Safe to distribute.
    public_key_path: Path = Path("keys/public.pem")

    #: Identifier for the active key, published as the ``kid`` header. Verifiers
    #: use it to select a key, which is what makes rotation possible without a
    #: flag day.
    active_key_id: str = "primary"

    #: Directory of retired public keys (``<kid>.pem``), still served from JWKS
    #: and still accepted for verification. Rotation without this directory
    #: invalidates every outstanding token the moment the key changes.
    retired_keys_dir: Path = Path("keys/retired")

    #: ``iss`` claim. Lets consumers reject foreign tokens.
    issuer: str = "genesis"

    #: ``aud`` claim. Scopes a token to a specific consumer.
    audience: str = "genesis-api"

    #: Short by design: an access token cannot be revoked, so its lifetime *is*
    #: the revocation window. Anything beyond ~15 minutes trades security for a
    #: refresh call the client has to make anyway.
    access_token_expire_minutes: int = 15

    #: Refresh tokens are revocable (server-side session records), so they can
    #: safely live much longer.
    refresh_token_expire_days: int = 30

    #: Allowed clock skew (seconds) when validating ``exp``/``nbf``.
    leeway_seconds: int = 0

    #: Cache lifetime advertised on the JWKS endpoint.
    jwks_cache_seconds: int = 3600


class SecuritySettings(BaseModel):
    """Password policy and session/revocation behaviour.

    Policy, not mechanism: the hashing and signing primitives live in
    :mod:`app.core.security`. These values decide what the *application*
    considers acceptable, and belong in configuration because they legitimately
    differ between a developer's machine and a regulated production tenant.
    """

    #: Length is the only password rule with strong evidence behind it. NIST
    #: 800-63B explicitly recommends against forced composition rules and
    #: scheduled expiry — both push users toward predictable patterns.
    password_min_length: int = 12

    #: Upper bound. Argon2 cost scales with input, so an unbounded password
    #: field is a cheap denial-of-service vector.
    password_max_length: int = 128

    #: Composition requirements. Off by default, present because compliance
    #: regimes sometimes mandate them regardless of the evidence.
    password_require_uppercase: bool = False
    password_require_lowercase: bool = False
    password_require_digit: bool = False
    password_require_symbol: bool = False

    #: Reject passwords appearing in a known-breach corpus. Far more effective
    #: than composition rules — it blocks the passwords actually being tried.
    password_check_breached: bool = True

    #: Previous hashes retained to prevent immediate reuse. 0 disables.
    password_history_depth: int = 0

    #: Bumping a user's token version invalidates every access token already
    #: issued to them. This is the only way to revoke a stateless token before
    #: it expires — used for logout-everywhere, password change and lockout.
    token_version_claim: str = "tv"  # noqa: S105 - a claim name, not a secret

    #: Failed attempts before an account is temporarily locked.
    max_failed_login_attempts: int = 10

    #: Lockout duration once the threshold is hit.
    lockout_duration_minutes: int = 15


class RateLimitSettings(BaseModel):
    """Request rate limiting, backed by Redis.

    Disabled by default so local development is not throttled; it must be on in
    any internet-facing environment.
    """

    enabled: bool = False

    #: Requests permitted per window for an unauthenticated caller, keyed by IP.
    anonymous_per_minute: int = 60

    #: Requests permitted per window for an authenticated caller, keyed by user.
    authenticated_per_minute: int = 600

    #: Sliding window length in seconds.
    window_seconds: int = 60

    #: Paths never rate limited. Health probes must answer even while an
    #: instance is being hammered, or the orchestrator will kill a healthy pod.
    exempt_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/live", "/ready", "/metrics"]
    )

    #: Allow requests through when Redis is unreachable. Failing open trades
    #: enforcement for availability; failing closed does the reverse. Open is
    #: the right default for a limiter protecting against accident, not attack.
    fail_open: bool = True


class RedisSettings(BaseModel):
    """Redis connection settings shared by cache, pub/sub, queue and limiter.

    One URL is intentionally shared; separate concerns by key prefix rather
    than by running multiple clusters.
    """

    url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    #: Namespace prepended to every key. A shared Redis without a namespace is
    #: how a staging deploy silently reads production cache entries.
    key_prefix: str = "genesis"

    #: Default TTL (seconds) when a cache write omits one.
    default_ttl_seconds: int = 300

    max_connections: int = 20
    socket_timeout_seconds: float = 5.0


class StorageSettings(BaseModel):
    """Object storage (S3-compatible) configuration.

    Consumed by :mod:`app.infrastructure.storage.providers`; the provider
    discriminator decides which implementation is built at startup.
    """

    provider: Literal["local", "s3"] = "local"

    #: Filesystem root for the local provider. Development only.
    local_root: Path = Path("var/storage")

    bucket: str | None = None
    region: str | None = None

    #: Custom endpoint for S3-compatible services (MinIO, R2, Spaces).
    endpoint_url: str | None = None

    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None

    #: Lifetime of generated presigned URLs.
    presigned_url_expire_seconds: int = 900

    #: Largest accepted upload. Enforce at the proxy too — by the time the app
    #: sees an oversized body it has already been buffered.
    max_upload_bytes: int = 10 * 1024 * 1024


class EmailSettings(BaseModel):
    """Outbound email transport and rendering configuration."""

    provider: Literal["console", "smtp"] = "console"

    from_address: str = "no-reply@example.com"
    from_name: str = "Genesis"
    reply_to: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True

    template_dir: Path = Path("app/infrastructure/email/templates")


class LoggingSettings(BaseModel):
    """Log verbosity, format and redaction.

    Consumed by :mod:`app.core.logging` during bootstrap, before the app exists,
    so it must not depend on anything that logs.
    """

    level: LogLevel = "INFO"

    #: JSON is required wherever a log aggregator indexes fields. The plain
    #: renderer is only for a human tailing a terminal.
    json_format: bool = False

    #: Emit an access log line per request.
    access_log: bool = True

    #: Log request and response bodies. Development only: bodies contain
    #: passwords, tokens and personal data, and redaction is best-effort.
    log_request_body: bool = False

    #: Paths excluded from the access log. Health probes fire every few seconds
    #: and would otherwise dominate log volume and cost.
    exclude_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/live", "/ready", "/metrics"]
    )


class ObservabilitySettings(BaseModel):
    """Metrics and tracing.

    Off by default. The seams exist from day one because retrofitting
    instrumentation means touching every layer, while enabling a seam that is
    already in place is a configuration change.
    """

    #: Expose Prometheus metrics at ``/metrics``.
    metrics_enabled: bool = False

    #: Emit OpenTelemetry spans.
    tracing_enabled: bool = False

    #: OTLP collector endpoint.
    otlp_endpoint: str | None = None

    #: Service name reported to the tracing backend.
    service_name: str = "genesis-api"

    #: Fraction of traces sampled. 1.0 is fine until it is not — full sampling
    #: at scale costs more than the rest of the observability stack combined.
    trace_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    #: Report unhandled exceptions to an error tracker.
    sentry_dsn: SecretStr | None = None


class Settings(BaseSettings):
    """Root settings object aggregating every configuration block.

    Instantiate exactly once — see :mod:`app.core.config`.
    """

    model_config = SettingsConfigDict(
        # Later files win, so the environment-specific overlay overrides .env.
        env_file=(".env", f".env.{_ENVIRONMENT}"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        secrets_dir=_SECRETS_DIR,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings
    jwt: JWTSettings = Field(default_factory=JWTSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Self:
        """Reject configurations that are unsafe in production.

        Every check here corresponds to a real, repeated production incident:
        a debug traceback leaking source, a wildcard CORS policy allowing any
        site to make authenticated requests, an unthrottled public API, logs
        that no aggregator can parse.

        Crashing at boot is the point. A misconfigured instance that refuses to
        start is a failed deploy; one that starts is a breach.

        Raises:
            ValueError: When a production-unsafe setting is detected.
        """
        if not self.app.is_production:
            return self

        problems: list[str] = []

        if self.app.debug:
            problems.append("APP__DEBUG must be false in production")
        if "*" in self.app.cors_origins:
            problems.append("APP__CORS_ORIGINS must not contain '*' in production")
        if not self.app.trusted_hosts:
            problems.append("APP__TRUSTED_HOSTS must be set in production")
        if not self.logging.json_format:
            problems.append("LOGGING__JSON_FORMAT must be true in production")
        if self.logging.log_request_body:
            problems.append("LOGGING__LOG_REQUEST_BODY must be false in production")
        if not self.rate_limit.enabled:
            problems.append("RATE_LIMIT__ENABLED must be true in production")
        if self.database.echo:
            problems.append("DATABASE__ECHO must be false in production")

        if problems:
            raise ValueError(
                "Unsafe production configuration:\n  - " + "\n  - ".join(problems)
            )
        return self
