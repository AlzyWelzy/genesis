"""Central logging configuration.

Why this file exists
--------------------
Libraries configure loggers; applications configure *handlers*. If each module
called ``basicConfig`` the result would be duplicated lines, inconsistent
formats and level fights between dependencies. This module owns the root
logger, and it is called exactly once from the application lifespan before
anything else runs.

What makes logs actually useful
-------------------------------
Three decisions, all enforced here rather than left to call sites:

1. **Structured output.** One JSON object per line in deployed environments, so
   an aggregator indexes fields instead of regex-parsing prose. A human tailing
   a terminal gets a readable renderer instead — the format is configuration,
   not a call-site choice.
2. **Automatic correlation.** Every record carries the request and correlation
   IDs from :mod:`app.core.context`. This is the difference between "an error
   happened" and "here are the fourteen log lines belonging to the request that
   failed" — and it costs the caller nothing, because a filter injects it.
3. **Redaction.** Anything that looks like a credential is masked before it is
   written. Best-effort, so it is a safety net rather than a licence to log
   sensitive data.

Usage::

    logger = get_logger(__name__)
    logger.info("Provisioning workspace", extra={"workspace_id": str(ws.id)})

Pass context via ``extra``, never by formatting it into the message. A message
that varies per call cannot be grouped, counted or alerted on.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from app.common.constants import REDACTED, SENSITIVE_FIELD_NAMES
from app.core.config import settings
from app.core.context import correlation_id_var, request_id_var, tenant_id_var

#: Third-party loggers that are noisy at INFO and better raised a level.
_NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn.access",
    "sqlalchemy.engine",
    "asyncio",
    "botocore",
    "aiobotocore",
    "urllib3",
)

#: ``LogRecord`` attributes that are not application context and must be
#: stripped before serialising the remainder as structured extras.
_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class ContextFilter(logging.Filter):
    """Attach the ambient request context to every record.

    Implemented as a filter rather than asking callers to pass IDs, because a
    convention that must be remembered at ten thousand call sites is a
    convention that will be missed at the one that matters.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach context IDs and always allow the record through."""
        record.request_id = request_id_var.get()
        record.correlation_id = correlation_id_var.get()
        tenant_id = tenant_id_var.get()
        record.tenant_id = str(tenant_id) if tenant_id else None
        record.trace_id = _current_trace_id()
        return True


def _current_trace_id() -> str | None:
    """Return the active trace ID, or ``None`` when tracing is not running.

    This is the half of trace/log correlation that lives on the log side.
    Without it a trace viewer can show that a request was slow but not why: the
    jump from a slow span to that request's log lines is exactly the step an
    investigation needs, and it only works if the ID is on both.

    Imported lazily for two reasons. ``app.infrastructure.observability.tracing``
    imports :func:`get_logger` from this module, so a top-level import closes a
    cycle. And tracing is an optional extra — resolving it at import time would
    make a deployment without it pay for a module it will never use.

    Never raises. A logging filter that can fail takes down logging itself,
    which is the last thing that should break while something else is going
    wrong.
    """
    try:
        from app.infrastructure.observability.tracing import (  # noqa: PLC0415 - cycle
            current_trace_id,
        )

        return current_trace_id()
    except Exception:  # noqa: BLE001 - logging must never be the thing that fails
        return None


def _redact(value: Any, key: str | None = None) -> Any:
    """Recursively mask values whose key looks sensitive.

    Best-effort by design: it catches the accidental
    ``extra={"payload": request_body}`` that would otherwise write a password
    to disk. It is not a substitute for not logging secrets.
    """
    if key and key.lower() in SENSITIVE_FIELD_NAMES:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class JSONFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Keywords passed via ``extra`` are merged into the top level of the object,
    which is what makes structured querying possible downstream ("every 500 for
    tenant X in the last hour").
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
            if key not in _RESERVED_RECORD_KEYS and value is not None:
                payload[key] = _redact(value, key)

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable renderer for local development.

    Appends the request ID when present, so concurrent requests can still be
    told apart while tailing a terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a readable line."""
        base = super().format(record)
        request_id = getattr(record, "request_id", None)
        return f"{base} [{request_id}]" if request_id else base


def configure_logging() -> None:
    """Install the root handler and align third-party log levels.

    Idempotent: existing root handlers are replaced, so calling this twice (for
    example under an auto-reloading dev server) does not duplicate output. Must
    be called before the first log line is emitted.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JSONFormatter()
        if settings.logging.json_format
        else ConsoleFormatter(
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

    Prefer ``get_logger(__name__)`` over ``logging.getLogger(__name__)`` so
    there is one import path to change if the logging backend is ever swapped
    for structlog or similar.
    """
    return logging.getLogger(name)
