"""Application startup and shutdown orchestration.

Why this file exists
--------------------
Long-lived resources — the database engine, the Redis pool, the metrics
registry — must be created once per process and, more importantly, *released*
before it exits. Creating them at import time makes the application impossible
to test and leaks connections on shutdown; creating them per request destroys
throughput.

The ASGI lifespan protocol gives exactly one place to do this correctly.
Everything before ``yield`` runs before the first request is accepted;
everything after runs once the last response is sent.

Ordering rules
--------------
* **Acquire in dependency order.** Logging first — it is needed to report the
  failure of everything else.
* **Release in reverse order, defensively.** A failure disposing one resource
  must not strand the others, so each teardown step is independently guarded.
* **Fail fast on startup.** A process that cannot reach its database should
  crash and be restarted, not start and serve 503s. Verifying connectivity here
  turns a misconfiguration into a failed deploy — which is exactly what a
  rolling deployment is designed to catch and halt.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import get_signing_key
from app.infrastructure.database.session import check_database_health, dispose_engine
from app.infrastructure.observability.metrics import configure_metrics
from app.infrastructure.observability.tracing import configure_tracing, shutdown_tracing
from app.infrastructure.redis.client import close_redis, init_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the lifetime of process-wide resources.

    Args:
        _app: The application instance, supplied by the ASGI server. Unused:
            resources are module-level singletons reached through
            dependencies rather than stashed on ``app.state``.

    Yields:
        Control to the server for the duration of the application's life.

    Raises:
        RuntimeError: When a required dependency is unreachable at startup.
    """
    configure_logging()
    logger.info(
        "Starting %s v%s",
        settings.app.name,
        settings.app.version,
        extra={"environment": settings.app.environment},
    )

    configure_metrics()
    configure_tracing()

    # Load and cache the signing key now. Failing here is far better than
    # discovering a missing or unreadable key on the first login attempt.
    get_signing_key()

    if not await check_database_health():
        raise RuntimeError(
            "Database is unreachable at startup. Refusing to start: an instance "
            "that cannot reach its database must fail the deploy, not serve errors."
        )
    logger.info("Database ready")

    # Redis is optional at startup. The cache and rate limiter degrade rather
    # than fail, so a Redis outage must not block a deploy.
    try:
        await init_redis()
    except Exception:  # noqa: BLE001 - degraded startup is acceptable here
        logger.warning("Redis unavailable at startup; continuing degraded")

    # TODO: build the configured storage and email providers and publish them.
    # TODO: start the queue consumer / scheduler task group (Stage 2).
    # TODO: import the modules that register event handlers, so subscriptions
    # exist before the first request — decorators only run on import.

    logger.info("Startup complete")

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app.name)

        # Each step is independently guarded: a failure closing one resource
        # must not prevent the rest from being released.
        for name, close in (
            ("redis", close_redis),
            ("database", dispose_engine),
        ):
            try:
                await close()
            except Exception:
                logger.exception("Error closing %s", name)

        try:
            shutdown_tracing()
        except Exception:
            logger.exception("Error flushing traces")

        logger.info("Shutdown complete")
