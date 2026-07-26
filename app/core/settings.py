"""Typed application configuration.

Why this file exists
--------------------
Configuration is the single most common source of environment drift in a large
SaaS platform: a value read from ``os.environ`` deep inside a service is
invisible, untyped and impossible to validate at boot. This module declares the
*entire* configuration surface of the application as nested Pydantic models so
that:

* every setting has a type, a default (or is explicitly required) and a docstring;
* an invalid or missing value fails loudly at startup, never at 3am in a request;
* subsystems own their own settings block instead of sharing one flat namespace;
* tests can construct a ``Settings`` instance directly without touching the env.

This module only *declares* the shape. Instantiation happens exactly once in
:mod:`app.core.config` — import ``settings`` from there, never from here.

Environment variables
---------------------
Nested models are populated with a double-underscore delimiter::

    APP__NAME=Genesis
    DATABASE__URL=postgresql+asyncpg://user:pass@localhost/genesis
    JWT__ACCESS_TOKEN_EXPIRE_MINUTES=15

Values are loaded from the process environment first, then ``.env``.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

type Environment = Literal["local", "development", "staging", "production"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class AppSettings(BaseModel):
    """Identity and runtime behaviour of the ASGI application itself.

    Nothing in here talks to an external system; these values decide how the
    app presents itself (title, docs, version) and which safety rails are on.
    """

    name: str = "Genesis"
    version: str = "0.1.0"
    description: str = "Genesis API"
    environment: Environment = "local"

    #: Root path for every API route. Keep versioning in the feature routers.
    api_prefix: str = "/api"

    #: Enables interactive docs and verbose errors. Must be False in production.
    debug: bool = False

    #: Origins allowed by the CORS middleware. Never use ["*"] with credentials.
    cors_origins: list[str] = Field(default_factory=list)

    # TODO: add `root_path` once the service runs behind a path-stripping proxy.
    # TODO: add `trusted_hosts` for the TrustedHostMiddleware in production.

    @property
    def is_production(self) -> bool:
        """Whether the app runs in the production environment."""
        return self.environment == "production"

    @property
    def docs_url(self) -> str | None:
        """OpenAPI docs path, disabled outside of non-production environments."""
        return None if self.is_production else "/docs"

    @property
    def openapi_url(self) -> str | None:
        """OpenAPI schema path, disabled outside of non-production environments."""
        return None if self.is_production else "/openapi.json"


class DatabaseSettings(BaseModel):
    """PostgreSQL connection and async engine pool tuning.

    Owned by :mod:`app.infrastructure.database.session`, which is the only
    module allowed to read these values.
    """

    #: Must use the asyncpg driver: postgresql+asyncpg://...
    url: PostgresDsn

    #: Emit every statement to the logger. Development only — very noisy.
    echo: bool = False

    #: Connections kept open per worker process.
    pool_size: int = 10

    #: Extra connections allowed above `pool_size` during bursts.
    max_overflow: int = 10

    #: Seconds to wait for a free connection before raising.
    pool_timeout: int = 30

    #: Recycle connections older than this (seconds) to survive proxy timeouts.
    pool_recycle: int = 1800

    #: Ping a connection before handing it out. Costs a round trip, prevents
    #: handing out sockets killed by a load balancer.
    pool_pre_ping: bool = True

    # TODO: add a separate `read_replica_url` once read/write splitting lands.
    # TODO: add `statement_timeout` and wire it into the engine connect_args.


class JWTSettings(BaseModel):
    """Asymmetric token signing material and lifetimes.

    Asymmetric (EdDSA/Ed25519) signing is the default so that services which
    only *verify* tokens never need the private key. Consumed exclusively by
    :mod:`app.core.security`.
    """

    #: EdDSA (Ed25519) by default. RS256 is supported for legacy interop.
    algorithm: Literal["EdDSA", "RS256", "ES256"] = "EdDSA"

    #: PEM file used to sign tokens. Must never be committed or shipped to
    #: verify-only services.
    private_key_path: Path = Path("keys/private.pem")

    #: PEM file used to verify tokens. Safe to distribute.
    public_key_path: Path = Path("keys/public.pem")

    #: `iss` claim. Used by consumers to reject foreign tokens.
    issuer: str = "genesis"

    #: `aud` claim. Used to scope a token to a specific consumer.
    audience: str = "genesis-api"

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    #: Allowed clock skew (seconds) when validating `exp` / `nbf`.
    leeway_seconds: int = 0

    # TODO: add `key_id` (kid) and a key set once key rotation is implemented.


class RedisSettings(BaseModel):
    """Redis connection settings shared by cache, pub/sub and queue.

    A single Redis URL is intentionally shared; separate logical concerns by
    key prefix and database index rather than by running multiple clusters.
    """

    url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    #: Namespace prepended to every key written by the cache abstraction.
    key_prefix: str = "genesis"

    #: Default TTL (seconds) applied when a cache write omits one.
    default_ttl_seconds: int = 300

    max_connections: int = 20
    socket_timeout_seconds: float = 5.0

    # TODO: add `sentinel_hosts` / `cluster_mode` when moving off a single node.


class StorageSettings(BaseModel):
    """Object storage (S3-compatible) configuration.

    Consumed by :mod:`app.infrastructure.storage.providers`. The provider
    discriminator decides which concrete implementation is built at startup.
    """

    provider: Literal["local", "s3"] = "local"

    #: Filesystem root used by the local provider. Development only.
    local_root: Path = Path("var/storage")

    bucket: str | None = None
    region: str | None = None

    #: Custom endpoint for S3-compatible services (MinIO, R2, Spaces).
    endpoint_url: str | None = None

    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None

    #: Lifetime of generated presigned download/upload URLs.
    presigned_url_expire_seconds: int = 900

    # TODO: add `public_base_url` for serving assets through a CDN.


class EmailSettings(BaseModel):
    """Outbound email transport and rendering configuration.

    Consumed by :mod:`app.infrastructure.email.providers`.
    """

    provider: Literal["console", "smtp"] = "console"

    #: Envelope sender used when a message does not override it.
    from_address: str = "no-reply@example.com"
    from_name: str = "Genesis"

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True

    #: Directory containing the message templates.
    template_dir: Path = Path("app/infrastructure/email/templates")

    # TODO: add `reply_to` and per-tenant sender overrides.


class LoggingSettings(BaseModel):
    """Log verbosity and output format.

    Consumed by :mod:`app.core.logging` during bootstrap, before the app is
    created, so it must not depend on anything that logs.
    """

    level: LogLevel = "INFO"

    #: JSON is required in any environment with a log aggregator; the plain
    #: renderer is only readable for a human tailing a terminal.
    json_format: bool = False

    #: Emit an access log line per request. Disable when a proxy already does.
    access_log: bool = True

    # TODO: add `sentry_dsn` / OTLP endpoint when observability is wired up.


class Settings(BaseSettings):
    """Root settings object aggregating every configuration block.

    Instantiate this exactly once — see :mod:`app.core.config`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings
    jwt: JWTSettings = Field(default_factory=JWTSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # TODO: add a model_validator that rejects debug=True and empty CORS
    # wildcards when app.environment == "production".
