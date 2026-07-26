"""Middleware registration.

Why this file exists
--------------------
Middleware order is semantic, not cosmetic. The last middleware added is the
outermost layer, so an error-handling middleware registered after a logging one
will never see the logs it was meant to annotate. Scattering ``add_middleware``
calls across modules makes that order emergent and unreviewable.

This module is the single ordered list. ``main.py`` calls
:func:`register_middleware` and nothing else touches the stack.

What belongs here
-----------------
Cross-cutting concerns that must observe *every* request: correlation IDs,
timing, CORS, compression, host validation, rate limiting. What does **not**
belong here: anything that needs to know about a specific feature, and anything
that can be expressed as a router dependency — dependencies are testable in
isolation and only cost what they are used for.
"""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import settings


def register_middleware(app: FastAPI) -> None:
    """Attach every application middleware in the correct order.

    Registration order is inside-out: the middleware added *last* wraps all the
    others and sees the request first.

    Args:
        app: The application to configure.
    """
    if settings.app.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.app.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    # TODO: RequestContextMiddleware — read or mint X-Request-ID, push it into
    # a ContextVar consumed by app.core.logging, echo it on the response.
    # TODO: TimingMiddleware — record request duration and emit a metric.
    # TODO: GZipMiddleware — only if responses are not already compressed at
    # the edge; double compression wastes CPU.
    # TODO: TrustedHostMiddleware — bind Host headers in production.
    # TODO: RateLimitMiddleware — backed by app.infrastructure.redis.
