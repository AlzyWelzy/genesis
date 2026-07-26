"""Operational endpoints: health probes and key distribution.

Why these are not one endpoint
------------------------------
Kubernetes asks two different questions and reacts very differently to the
answers. Conflating them causes outages, and the failure mode is
counter-intuitive enough to be worth stating precisely.

``/live`` — **liveness**: "is this process wedged?" A failure gets the container
**killed and restarted**. It must therefore check *nothing external*. If it
checked the database, a database blip would fail every replica's liveness probe
at once, restarting the entire fleet — turning a recoverable dependency outage
into a total outage, and adding a thundering herd of reconnects on recovery.

``/ready`` — **readiness**: "can this instance serve traffic right now?" A
failure removes the pod from the load balancer but leaves it running. This is
where dependencies are checked: an instance that cannot reach the database
should stop receiving requests, and should start receiving them again when it
recovers, with no restart.

``/health`` — a human-readable summary for dashboards and on-call. Never wire an
orchestrator to it.

These endpoints are mounted at the root, outside the API prefix and outside
versioning. Probe URLs live in deployment manifests that are updated on a
different cadence than the application, and a liveness probe that 404s because
the API prefix changed will get every replica killed.
"""

from typing import Any, Final

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.security import build_jwks
from app.infrastructure.database.session import check_database_health
from app.infrastructure.observability.metrics import PrometheusMetrics, get_metrics
from app.infrastructure.redis.client import check_redis_health
from app.system.state import is_started

router = APIRouter(tags=["system"])

#: Excluded from the access log and the rate limiter — see the settings for
#: both. Probes fire every few seconds per replica.
PROBE_PATHS: Final[tuple[str, ...]] = ("/health", "/live", "/ready", "/metrics")


@router.get("/live", summary="Liveness probe", include_in_schema=False)
async def live() -> dict[str, str]:
    """Report that the process is running and its event loop responsive.

    Checks nothing external, deliberately. See this module's docstring: a
    liveness probe that depends on the database restarts the fleet during a
    database incident.
    """
    return {"status": "alive"}


@router.get("/ready", summary="Readiness probe", include_in_schema=False)
async def ready(response: Response) -> dict[str, Any]:
    """Report whether this instance can serve traffic.

    Checks the dependencies a request genuinely cannot proceed without. Returns
    503 when any is unavailable, which removes this instance from the load
    balancer without restarting it.

    Redis is treated as non-fatal: the cache and rate limiter both degrade
    rather than fail, so an instance without Redis can still serve requests.
    Marking it fatal would take the whole service offline for a component the
    application is designed to survive.
    """
    database_ok = await check_database_health()
    redis_ok = await check_redis_health()

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if database_ok else "not_ready",
        "checks": {
            "database": "ok" if database_ok else "unavailable",
            "redis": "ok" if redis_ok else "degraded",
        },
    }


@router.get("/health", summary="Health summary", include_in_schema=False)
async def health() -> dict[str, Any]:
    """Human-readable status summary for dashboards and on-call.

    Deliberately unauthenticated and deliberately vague: it names the service
    and version but no hostnames, dependency addresses or error text. A health
    endpoint is one of the most-scanned URLs on any public service.
    """
    return {
        "status": "ok",
        "service": settings.app.name,
        "version": settings.app.version,
        "environment": settings.app.environment,
    }


@router.get(
    "/.well-known/jwks.json",
    summary="JSON Web Key Set",
    response_model=None,
)
async def jwks() -> dict[str, Any]:
    """Publish the public keys used to verify tokens issued by this service.

    Lets other services, gateways and partners verify tokens without a copied
    PEM file, and lets them pick up key rotations on their own. Exposes public
    keys only — the private key is never loaded by this path.
    """
    return build_jwks()


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Expose metrics in the Prometheus exposition format.

    Returns 404 when metrics are disabled, so a scraper gets an unambiguous
    answer rather than an empty 200 that looks like a healthy service reporting
    nothing.

    **Do not expose this publicly.** Request rates, error counts and endpoint
    names per route are useful reconnaissance. Bind it to an internal port, or
    put it behind the ingress rules that already protect the admin surface.
    """
    recorder = get_metrics()
    if not isinstance(recorder, PrometheusMetrics):
        raise NotFoundError("Metrics are not enabled on this instance.")

    body, content_type = recorder.render()
    return Response(content=body, media_type=content_type)


@router.get("/startup", summary="Startup probe", include_in_schema=False)
async def startup(response: Response) -> dict[str, Any]:
    """Report whether the application has finished initialising.

    Kubernetes suspends the liveness and readiness probes until this one
    succeeds. Without it, a boot that takes longer than the liveness
    ``failureThreshold`` is indistinguishable from a hung process, and the
    orchestrator kills the container — repeatedly, producing a crash loop whose
    only cause is that startup was slow.

    Separate from ``/live`` because they answer different questions at
    different times: this one is asked once and stops being asked, whereas
    liveness is asked forever.
    """
    ready = is_started()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "started" if ready else "starting"}
