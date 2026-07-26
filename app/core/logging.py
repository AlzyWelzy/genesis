"""Central logging configuration.

Why this file exists
--------------------
Libraries configure loggers; applications configure *handlers*. If each module
called ``basicConfig`` the result would be duplicated lines, inconsistent
formats and log level fights between dependencies. This module is the one place
that owns the root logger, and it is called exactly once from the application
lifespan before anything else runs.

Format is decided by configuration, not by the caller: humans tailing a
terminal get readable lines, deployed environments get one JSON object per line
so a log aggregator can index fields instead of regex-parsing prose.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from app.core.config import settings

#: Third-party loggers that are noisy at INFO and are better raised a level.
_NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn.access",
    "sqlalchemy.engine",
    "asyncio",
)

#: LogRecord attributes that are not application context and must be stripped
#: before serialising the remainder as structured extras.
_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JSONFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Any keyword passed via ``logger.info("...", extra={...})`` is merged into
    the top level of the object, which is what makes structured querying
    ("show me every 500 for tenant X") possible downstream.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` to a compact JSON string."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS:
                payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install the root handler and align third-party log levels.

    Idempotent: existing root handlers are replaced, so calling this twice
    (for example under an auto-reloading dev server) does not duplicate output.
    Must be called before the first log line is emitted.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JSONFormatter()
        if settings.logging.json_format
        else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.logging.level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))

    if not settings.logging.access_log:
        logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger.

    Prefer ``get_logger(__name__)`` over ``logging.getLogger(__name__)`` so the
    application has one import path to change if the logging backend is swapped
    for structlog or similar.
    """
    return logging.getLogger(name)


# TODO: add a `request_id` ContextVar and a filter injecting it into every
# record, so a single request can be traced across services.
# TODO: add a `bind()` helper for per-task structured context (tenant, user).
