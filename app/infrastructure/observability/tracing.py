"""Distributed tracing.

Why this file exists
--------------------
Once a request touches the API, the database, Redis, an object store and a
third-party API, "the request was slow" stops being a useful statement. A trace
breaks one request into timed spans and shows *which* of those five was slow —
and in a request that fans out, which of them ran in parallel and which
accidentally serialised.

Relationship to correlation IDs
-------------------------------
:mod:`app.core.context` already propagates a correlation ID, which links *log
lines* across services. Tracing links *timings*. They answer different questions
and both are worth having; where possible the trace ID should be recorded as a
log field so a slow span leads directly to its log lines.

Sampling
--------
Full sampling is affordable in development and ruinous in production — at scale
the tracing bill exceeds the rest of the observability stack. The default is
10%, with the caveat that the interesting requests (errors, slow outliers) are
exactly the ones a naive sampler is most likely to drop. Tail-based sampling,
which decides after the fact, is the fix.
"""

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def configure_tracing() -> None:
    """Initialise the tracing exporter.

    Called from the application lifespan. A no-op unless tracing is enabled.
    """
    if not settings.observability.tracing_enabled:
        return

    logger.info(
        "Tracing enabled",
        extra={
            "service_name": settings.observability.service_name,
            "sample_rate": settings.observability.trace_sample_rate,
        },
    )
    # TODO: add opentelemetry-sdk and the OTLP exporter, then:
    #   1. Build a TracerProvider with the service name as a resource attribute.
    #   2. Install the FastAPI, SQLAlchemy, Redis and httpx instrumentors —
    #      auto-instrumentation covers the boundaries that matter, so hand-rolled
    #      spans are only needed for business operations worth naming.
    #   3. Configure the sampler from trace_sample_rate.
    #   4. Add the trace ID to the log context so a span links to its log lines.


def shutdown_tracing() -> None:
    """Flush pending spans on shutdown.

    Exporters batch. Without an explicit flush the final spans — including any
    from the request that triggered a crash — are lost, which is precisely when
    they are most wanted.
    """
    if not settings.observability.tracing_enabled:
        return
    # TODO: call TracerProvider.shutdown() to force a final export.
