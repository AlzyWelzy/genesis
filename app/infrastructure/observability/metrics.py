"""Application metrics.

Why this file exists
--------------------
Metrics answer questions logs cannot: how many requests per second, what the
p99 latency is, how deep the queue is right now. Logs answer "what happened to
this one request"; metrics answer "what is happening to the system".

The design problem is that a metrics client is a global mutable singleton, and
scattering ``prometheus_client`` calls through business logic couples every
service to it — services then cannot be tested without a registry, and
switching backends means touching all of them.

The seam
--------
:class:`Metrics` is a protocol. :class:`NoOpMetrics` satisfies it and does
nothing, which is the default, so instrumented code runs unchanged whether
metrics are enabled or not. :class:`PrometheusMetrics` slots in behind the same
interface when ``OBSERVABILITY__METRICS_ENABLED`` is turned on.

Cardinality
-----------
The one rule worth stating loudly: **never label a metric with an unbounded
value.** User IDs, tenant IDs, request IDs and raw URL paths each create a new
time series per distinct value. A metric labelled with a user ID on a
million-user system is a million series, and it will take down the metrics
backend well before it takes down the application.

Use the *route template* (``/invoices/{id}``), never the resolved path. The
label sets below are fixed for exactly this reason.
"""

from typing import Any, Final, Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Latency buckets in seconds. Chosen around the thresholds that matter for an
#: API: sub-100ms feels instant, 1s is noticeable, 10s is a timeout in most
#: clients. Prometheus histograms cost one series per bucket per label set, so
#: this is deliberately short.
LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Metric names. Suffixes follow Prometheus convention: `_total` for counters,
#: `_seconds` for durations. Exporters and dashboards rely on it.
HTTP_REQUESTS = "http_requests_total"
HTTP_DURATION = "http_request_duration_seconds"
DB_POOL_IN_USE = "db_pool_connections_in_use"
QUEUE_DEPTH = "queue_depth"
JOBS_PROCESSED = "jobs_processed_total"
CACHE_OPERATIONS = "cache_operations_total"


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
    regardless of whether a backend is configured, which is what stops
    instrumentation from being stripped out "because we don't use it yet".
    """

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Discard a counter increment."""

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Discard a histogram observation."""

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Set a gauge value and discard it."""


class RecordingMetrics:
    """Records everything in memory for assertions.

    For tests: lets a test check that an operation emitted the metric it should
    have, without a registry or a scrape.
    """

    def __init__(self) -> None:
        self.counters: list[tuple[str, int, dict[str, str]]] = []
        self.observations: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Record a counter increment."""
        self.counters.append((name, value, labels))

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record a histogram observation."""
        self.observations.append((name, value, labels))

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Record a gauge value."""
        self.gauges.append((name, value, labels))

    def clear(self) -> None:
        """Discard everything recorded. For test teardown."""
        self.counters.clear()
        self.observations.clear()
        self.gauges.clear()


class PrometheusMetrics:
    """Prometheus-backed implementation.

    Metric objects are created **once** in :meth:`__init__` and reused.
    Constructing them per call raises a duplicate-registration error, which is
    the first thing anyone hits when wiring ``prometheus_client`` by hand.

    Label names are fixed per metric at construction, so a caller passing an
    unexpected label gets an error rather than silently creating a new series.

    Requires the ``metrics`` extra: ``uv sync --extra metrics``.
    """

    def __init__(self) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        self.registry = CollectorRegistry()
        self._counters: dict[str, Any] = {
            HTTP_REQUESTS: Counter(
                HTTP_REQUESTS,
                "Total HTTP requests.",
                ["method", "route", "status"],
                registry=self.registry,
            ),
            JOBS_PROCESSED: Counter(
                JOBS_PROCESSED,
                "Total background jobs processed.",
                ["task", "outcome"],
                registry=self.registry,
            ),
            CACHE_OPERATIONS: Counter(
                CACHE_OPERATIONS,
                "Total cache operations.",
                ["operation", "outcome"],
                registry=self.registry,
            ),
        }
        self._histograms: dict[str, Any] = {
            HTTP_DURATION: Histogram(
                HTTP_DURATION,
                "HTTP request duration in seconds.",
                ["method", "route"],
                buckets=LATENCY_BUCKETS,
                registry=self.registry,
            ),
        }
        self._gauges: dict[str, Any] = {
            DB_POOL_IN_USE: Gauge(
                DB_POOL_IN_USE,
                "Database connections currently checked out.",
                registry=self.registry,
            ),
            QUEUE_DEPTH: Gauge(
                QUEUE_DEPTH,
                "Jobs waiting in the queue.",
                ["queue"],
                registry=self.registry,
            ),
        }

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        """Increment a Prometheus counter, ignoring unregistered names."""
        if metric := self._counters.get(name):
            (metric.labels(**labels) if labels else metric).inc(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Observe into a Prometheus histogram."""
        if metric := self._histograms.get(name):
            (metric.labels(**labels) if labels else metric).observe(value)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        """Set a Prometheus gauge."""
        if metric := self._gauges.get(name):
            (metric.labels(**labels) if labels else metric).set(value)

    def render(self) -> tuple[bytes, str]:
        """Render the current values in the Prometheus exposition format.

        Returns:
            A ``(body, content_type)`` pair for the ``/metrics`` response.
        """
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return generate_latest(self.registry), CONTENT_TYPE_LATEST


#: Process-wide recorder. Replaced during startup when metrics are enabled.
metrics: Metrics = NoOpMetrics()


def configure_metrics() -> None:
    """Install the configured metrics backend.

    Called from the application lifespan. A no-op unless metrics are enabled.

    A missing ``prometheus_client`` is logged and the no-op recorder is kept:
    an observability dependency must never be the reason an application refuses
    to start.
    """
    if not settings.observability.metrics_enabled:
        return

    try:
        set_metrics(PrometheusMetrics())
    except ImportError:
        logger.warning(
            "Metrics enabled but prometheus-client is not installed; "
            "install the 'metrics' extra. Continuing without metrics."
        )
        return

    logger.info("Metrics enabled")


def set_metrics(recorder: Metrics) -> None:
    """Replace the process-wide metrics recorder.

    Separate from :func:`configure_metrics` so tests can install a
    :class:`RecordingMetrics` and assert on what was emitted.
    """
    global metrics  # noqa: PLW0603 - process-wide singleton by design
    metrics = recorder


def get_metrics() -> Metrics:
    """Return the active recorder."""
    return metrics
