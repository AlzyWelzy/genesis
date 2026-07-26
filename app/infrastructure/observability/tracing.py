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
:mod:`app.core.context` propagates a correlation ID, which links *log lines*
across services. Tracing links *timings*. They answer different questions and
both are worth having; :func:`current_trace_id` exists so a log record can carry
the trace ID and a slow span leads directly to its log lines.

Sampling
--------
Full sampling is affordable in development and ruinous in production — at scale
the tracing bill can exceed the rest of the observability stack. The default is
10%, with the caveat that the interesting requests (errors, slow outliers) are
exactly the ones a naive head sampler is most likely to drop. Tail-based
sampling, decided at the collector after the fact, is the fix; it is configured
in the collector rather than here.

Optional dependency
-------------------
Requires the ``tracing`` extra: ``uv sync --extra tracing``. Absent, every
function here is a no-op — an observability dependency must never be the reason
an application refuses to start.
"""

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Populated by :func:`configure_tracing` when tracing is active.
_provider: Any = None


def configure_tracing() -> None:
    """Initialise the tracing exporter and instrument the I/O boundaries.

    Called from the application lifespan. A no-op unless tracing is enabled.

    Auto-instrumentation covers the boundaries that matter — HTTP in, database
    out, cache out — so hand-written spans are only needed for business
    operations worth naming. Instrumenting by hand instead is how half the
    system ends up untraced.
    """
    global _provider  # noqa: PLW0603 - process-wide singleton by design

    if not settings.observability.tracing_enabled:
        return

    try:
        # Unresolvable until `uv sync --extra tracing` installs them; the
        # ImportError below is the supported path when they are absent.
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import (
            Resource,
        )
        from opentelemetry.sdk.trace import (
            TracerProvider,
        )
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )
    except ImportError:
        logger.warning(
            "Tracing enabled but OpenTelemetry is not installed; install the "
            "'tracing' extra. Continuing without tracing."
        )
        return

    resource = Resource.create(
        {
            "service.name": settings.observability.service_name,
            "service.version": settings.app.version,
            "deployment.environment": settings.app.environment,
        }
    )
    # ParentBased: honour an upstream caller's sampling decision so a trace is
    # never half-recorded across a service boundary, and sample independently
    # only when this service starts the trace.
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(
            TraceIdRatioBased(settings.observability.trace_sample_rate)
        ),
    )
    # Batched, not simple: a span export per span would add a network round trip
    # to every operation being measured.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.observability.otlp_endpoint)
        )
    )
    trace.set_tracer_provider(provider)
    _provider = provider

    _instrument()
    logger.info(
        "Tracing enabled",
        extra={
            "service_name": settings.observability.service_name,
            "sample_rate": settings.observability.trace_sample_rate,
        },
    )


def _instrument() -> None:
    """Attach the auto-instrumentors that are installed.

    Each is optional and independently guarded: a missing instrumentation
    package should cost that one integration, not all of them.
    """
    integrations = (
        ("fastapi", "opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
        (
            "sqlalchemy",
            "opentelemetry.instrumentation.sqlalchemy",
            "SQLAlchemyInstrumentor",
        ),
        ("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor"),
    )
    for name, module_path, class_name in integrations:
        try:
            module = __import__(module_path, fromlist=[class_name])
            getattr(module, class_name)().instrument()
        except Exception:  # noqa: BLE001 - one integration must not block the rest
            logger.debug("Tracing integration unavailable: %s", name)


def current_trace_id() -> str | None:
    """Return the active trace ID as a hex string, if a trace is in progress.

    Used to stamp the trace ID onto log records, so a slow span in a trace
    viewer leads straight to that request's log lines.
    """
    if _provider is None:
        return None
    try:
        from opentelemetry import trace
    except ImportError:
        return None

    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


def shutdown_tracing() -> None:
    """Flush pending spans on shutdown.

    Exporters batch. Without an explicit flush the final spans — including any
    from the request that triggered a crash — are lost, which is precisely when
    they are most wanted.
    """
    global _provider  # noqa: PLW0603 - process-wide singleton by design

    if _provider is None:
        return
    _provider.shutdown()
    _provider = None
