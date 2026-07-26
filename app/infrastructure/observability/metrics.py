"""Application metrics.

Why this file exists
--------------------
Metrics answer questions logs cannot: how many requests per second, what the
p99 latency is, how many jobs are queued right now. Logs answer "what happened
to this one request"; metrics answer "what is happening to the system".

The design problem is that a metrics client is a global mutable singleton, and
scattering ``prometheus_client`` calls through business logic couples every
service to it — services then cannot be tested without a registry, and
switching backends means touching all of them.

The seam
--------
:class:`Metrics` is a protocol. :class:`NoOpMetrics` satisfies it and does
nothing, which is the default, so instrumented code runs unchanged whether
metrics are enabled or not. A Prometheus implementation slots in behind the
same interface when ``OBSERVABILITY__METRICS_ENABLED`` is turned on.

Cardinality
-----------
The one rule worth stating loudly: **never label a metric with an unbounded
value.** User IDs, tenant IDs, request IDs and raw URL paths each create a new
time series per distinct value. A metric labelled with a user ID on a
million-user system is a million series, and it will take down the metrics
backend well before it takes down the application. Use the *route template*
(``/invoices/{id}``), never the resolved path.
"""

from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Metrics(Protocol):
    """Metrics recording interface.

    Deliberately small. The three primitives below express essentially every
    application metric worth having, and a narrow interface is one any backend
    can satisfy.
    """

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Increment a counter — a monotonically increasing total."""
        ...

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record a value in a histogram — durations and sizes."""
        ...

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Set a gauge — a value that goes up and down, like queue depth."""
        ...


class NoOpMetrics:
    """Metrics implementation that discards everything.

    The default. Lets instrumentation be written once and left in place
    regardless of whether a metrics backend is configured, which is what stops
    instrumentation from being stripped out "because we don't use it yet".
    """

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Discard a counter increment."""

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Discard a histogram observation."""

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Discard a gauge value."""


class PrometheusMetrics:
    """Prometheus-backed implementation.

    Requires ``prometheus-client``, which is not a dependency yet — add it when
    enabling metrics. Metric objects must be created once at import and reused;
    constructing them per call raises a duplicate-registration error.
    """

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Increment a Prometheus counter."""
        raise NotImplementedError

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Observe into a Prometheus histogram."""
        raise NotImplementedError

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Set a Prometheus gauge."""
        raise NotImplementedError


#: Process-wide recorder. Replaced during startup when metrics are enabled.
metrics: Metrics = NoOpMetrics()


def configure_metrics() -> None:
    """Install the configured metrics backend.

    Called from the application lifespan. A no-op unless metrics are enabled.
    """
    if not settings.observability.metrics_enabled:
        return

    logger.info("Metrics enabled")
    # TODO: add prometheus-client, declare the metric objects at module level,
    # then rebind the module-level recorder with `set_metrics(PrometheusMetrics())`
    # and expose the registry through the /metrics endpoint in app.system.router.


def set_metrics(recorder: Metrics) -> None:
    """Replace the process-wide metrics recorder.

    Separate from :func:`configure_metrics` so tests can install a recording
    fake and assert on what was emitted, without enabling a real backend.
    """
    global metrics  # noqa: PLW0603 - process-wide singleton by design
    metrics = recorder


# TODO: record the four signals worth alerting on before anything else —
# request rate, error rate, duration (by route template) and saturation
# (connection pool in use, queue depth). Instrument them in middleware rather
# than per endpoint, so coverage cannot drift as endpoints are added.
