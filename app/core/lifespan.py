"""Application startup and shutdown orchestration.

Why this file exists
--------------------
Long-lived resources — the database engine, the Redis pool, the object storage
client — must be created once per process and, more importantly, *closed*
before the process exits. Creating them at import time makes the application
impossible to test and leaks connections on shutdown; creating them per request
destroys throughput.

The ASGI lifespan protocol gives exactly one place to do this correctly, and
this module is it. Everything before ``yield`` runs before the first request is
accepted; everything after runs once the last response is sent.

Ordering rules
--------------
* Acquire in dependency order (logging first — it is needed to report failures
  in everything else).
* Release in reverse order, and release defensively: a failure disposing one
  resource must not prevent the others from being disposed.
* Startup failures should propagate. A process that cannot reach its database
  must crash and be restarted, not serve 500s.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the lifetime of process-wide resources.

    Args:
        app: The application instance. Use ``app.state`` to publish resources
            that middleware needs; prefer module-level singletons accessed
            through dependencies for everything else.

    Yields:
        Control to the server for the duration of the application's life.
    """
    configure_logging()
    logger.info(
        "Starting %s v%s [%s]",
        settings.app.name,
        settings.app.version,
        settings.app.environment,
    )

    # TODO: verify database connectivity (SELECT 1) and fail fast if unreachable.
    # TODO: create the Redis connection pool.
    # TODO: build the configured storage and email providers.
    # TODO: start the queue consumer / background worker task group.
    # TODO: register the event handlers declared in app.events.

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app.name)

        # TODO: stop accepting queue work and drain in-flight jobs.
        # TODO: close the Redis pool.
        # TODO: dispose the SQLAlchemy async engine.
        # Each teardown step belongs in its own try/except so one failure does
        # not strand the remaining resources.
