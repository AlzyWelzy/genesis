"""Middleware stack.

Why this file exists
--------------------
Middleware order is semantic, not cosmetic. Starlette applies middleware
outside-in in *reverse* registration order: the one added **last** wraps every
other and sees the request first. Get that wrong and a logging middleware
records durations that exclude the compression it was meant to measure, or an
error handler never sees the exception it was meant to catch.

Scattering ``add_middleware`` calls across modules makes that order emergent
and unreviewable. This module is the single ordered list, and the ordering
rationale is written down next to it.

What belongs here
-----------------
Concerns that must observe *every* request: correlation IDs, timing, CORS,
compression, host validation, rate limiting.

What does not: anything that needs to know about a specific feature, and
anything expressible as a router dependency. Dependencies are testable in
isolation, appear in the OpenAPI schema, and only cost what they are used for —
middleware runs on every request including the ones it is irrelevant to.
"""

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Match

from app.common.constants import CORRELATION_ID_HEADER, REQUEST_ID_HEADER
from app.core.config import settings
from app.core.context import correlation_id_var, get_user_id, request_id_var
from app.core.exceptions import RateLimitError, build_error_body
from app.core.logging import get_logger
from app.infrastructure.observability.metrics import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    get_metrics,
)
from app.infrastructure.redis.rate_limit import (
    EndpointLimit,
    RateLimitResult,
    check_rate_limit,
    check_token_bucket,
    limit_for_route,
)

logger = get_logger(__name__)

type CallNext = Callable[[Request], Awaitable[Response]]

#: Scope keys mirroring the context vars. Prefixed to avoid colliding with
#: anything ASGI or a future middleware might define.
REQUEST_ID_SCOPE_KEY = "genesis.request_id"
CORRELATION_ID_SCOPE_KEY = "genesis.correlation_id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Establish the request and correlation IDs for the request's lifetime.

    The request ID is minted here and identifies this single HTTP exchange. The
    correlation ID is taken from the inbound header when an upstream caller
    supplied one, so a user action that fans out across services shares one ID
    end to end; absent that, it mirrors the request ID.

    Both are pushed into :mod:`app.core.context` so every log record picks them
    up automatically, and both are echoed on the response so a client — or a
    user reporting a problem — can quote the ID that finds the log lines.

    The context is reset in a ``finally`` block: ``ContextVar`` values set
    inside a task outlive the request otherwise, and a worker reusing the task
    would inherit a stale ID.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        """Bind context IDs, call the app, and echo the IDs on the response."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or request_id

        # Also stashed on the scope. The ContextVar is reset in `finally`,
        # but Starlette's ServerErrorMiddleware sits *outside* this middleware,
        # so its handler runs after that reset. The scope survives, which is
        # what lets a 500 — the error where an ID matters most — still carry one.
        request.scope[REQUEST_ID_SCOPE_KEY] = request_id
        request.scope[CORRELATION_ID_SCOPE_KEY] = correlation_id

        request_token = request_id_var.set(request_id)
        correlation_token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            request_id_var.reset(request_token)
            correlation_id_var.reset(correlation_token)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured log line per request, with its duration.

    Structured rather than formatted: method, path, status and duration are
    separate fields, so an aggregator can answer "p99 latency of POST /invoices
    last hour" without parsing prose.

    Health probes are excluded. They fire every few seconds per replica and
    would otherwise dominate both log volume and its cost, drowning the lines
    that matter.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        """Time the request and log its outcome."""
        if request.url.path in settings.logging.exclude_paths:
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000

        # Labelled with the route *template*, never the resolved path. Labelling
        # by path would mint a new time series per invoice ID and take the
        # metrics backend down long before it touched the application.
        route = _route_template(request)
        recorder = get_metrics()
        recorder.increment(
            HTTP_REQUESTS,
            method=request.method,
            route=route,
            status=str(response.status_code),
        )
        recorder.observe(
            HTTP_DURATION,
            duration_ms / 1000,
            method=request.method,
            route=route,
        )

        logger.info(
            "%s %s %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "client_ip": request.client.host if request.client else None,
            },
        )
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle callers, backed by Redis so the limit is shared across replicas.

    An in-process limiter is close to useless behind a load balancer: with four
    replicas the effective limit is four times the configured one, and it
    resets on every deploy. The counter has to live somewhere shared.

    Fails open by configuration default. A limiter that blocks all traffic when
    Redis blinks has converted a cache outage into a full outage; for a limit
    protecting against accident rather than attack, that trade is wrong.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        """Check the caller's allowance before passing the request through."""
        if request.url.path in settings.rate_limit.exempt_paths:
            return await call_next(request)

        identity, authenticated = _resolve_identity(request)
        route = _prerouting_template(request)
        limit = limit_for_route(route, authenticated=authenticated)
        result = await _check(identity, route, limit)

        if not result.allowed:
            # Returned, NOT raised. An exception raised inside a
            # BaseHTTPMiddleware never reaches the application's exception
            # handlers — those live in ExceptionMiddleware, which sits *below*
            # this one in the stack. Raising here produces a 500 rather than a
            # 429, which is both wrong and actively misleading: the caller is
            # told the server broke when in fact it deliberately refused them.
            #
            # The envelope is built with the same helper the handlers use, so
            # the shape stays identical to every other error.
            error = RateLimitError(retry_after=result.reset_after)
            logger.info(
                "Rate limit exceeded",
                extra={"identity": identity, "http_path": request.url.path},
            )
            return JSONResponse(
                status_code=error.status_code,
                content=build_error_body(error.code, error.message),
                headers={**error.headers, **result.headers()},
            )

        response = await call_next(request)
        # Published on every response, not just rejections, so a well-behaved
        # client can slow down *before* it gets a 429.
        response.headers.update(result.headers())
        return response


async def _check(identity: str, route: str, limit: EndpointLimit) -> RateLimitResult:
    """Apply whichever algorithm the route's limit calls for.

    A per-route limit is keyed by ``identity`` *and* route. Keying it by
    identity alone would make the tight limit on one expensive endpoint share a
    counter with every ordinary read the same caller makes — so the expensive
    endpoint would be throttled by unrelated traffic, and its own allowance
    would silently consume everyone else's.

    The global limit keeps the bare identity as its key, so it still counts a
    caller's whole footprint across every route.
    """
    if limit.burst is None:
        return await check_rate_limit(
            identity,
            limit=limit.limit,
            window_seconds=limit.window_seconds,
        )

    return await check_token_bucket(
        f"{identity}:{route}",
        rate=limit.limit / limit.window_seconds,
        burst=limit.burst,
        window_seconds=limit.window_seconds,
    )


def _prerouting_template(request: Request) -> str:
    """Resolve the matched route's template *before* routing has run.

    Middleware sits above the router, so ``scope["route"]`` — which
    :func:`_route_template` reads — is not populated yet. Without resolving it
    here, a per-route rate limit can never apply, because the limiter runs at a
    point where it does not yet know which route it is limiting.

    Matching is done by asking each route whether it would accept the scope,
    which is exactly what the router itself is about to do, and costs a regex
    match per route. Falls back to ``"unmatched"``, which
    :func:`~app.infrastructure.redis.rate_limit.limit_for_route` maps to the
    global limit — so an unrecognised path is still throttled rather than
    exempt.
    """
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", None) or "unmatched"
    return "unmatched"


def _route_template(request: Request) -> str:
    """Return the matched route's path template, e.g. ``/invoices/{id}``.

    Falls back to ``"unmatched"`` rather than the raw path: a 404 sweep would
    otherwise create one metric series per URL an attacker tries, which is a
    trivially cheap way to exhaust the metrics backend.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def _resolve_identity(request: Request) -> tuple[str, bool]:
    """Determine what a request's rate limit is keyed by.

    Prefers the authenticated user: a user ID is attributable and stays stable
    across networks, whereas an IP may be shared by an entire office behind one
    NAT — throttling it punishes everyone there.

    Falls back to the client IP from the proxy's forwarded header. The raw
    socket address is wrong behind a proxy, where every request appears to come
    from the proxy itself and one caller's burst would throttle the whole
    service.

    Returns:
        An ``(identity, authenticated)`` pair.
    """
    if user_id := get_user_id():
        return f"user:{user_id}", True

    # Left-most entry is the original client; the rest are proxy hops. Only
    # trust this because TrustedHostMiddleware and the proxy sit in front — a
    # directly-exposed app must not, since the header is client-controlled.
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    return f"ip:{client_ip}", False


def register_middleware(app: FastAPI) -> None:
    """Attach every application middleware in the correct order.

    Registration is inside-out: the middleware added **last** is outermost and
    sees the request first. Reading the calls below bottom-to-top gives the
    order a request actually traverses:

    1. ``RequestContextMiddleware`` — outermost, so every subsequent layer
       (including the access log and any error handler) has the request ID.
    2. ``TrustedHostMiddleware`` — reject forged Host headers before doing any
       real work with them.
    3. ``CORSMiddleware`` — must be outside the app so it can answer preflight
       ``OPTIONS`` requests and, critically, so CORS headers are attached to
       *error* responses too. A 500 without CORS headers shows up in the
       browser as an opaque network failure with no clue as to the cause.
    4. ``AccessLogMiddleware`` — inside CORS, so the duration it records is the
       application's own work.
    5. ``GZipMiddleware`` — innermost, compressing the final body.

    Args:
        app: The application to configure.
    """
    app.add_middleware(GZipMiddleware, minimum_size=settings.app.gzip_minimum_size)

    if settings.rate_limit.enabled:
        app.add_middleware(RateLimitMiddleware)

    app.add_middleware(AccessLogMiddleware)

    if settings.app.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.app.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER, CORRELATION_ID_HEADER],
        )

    if settings.app.trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.app.trusted_hosts
        )

    app.add_middleware(RequestContextMiddleware)
