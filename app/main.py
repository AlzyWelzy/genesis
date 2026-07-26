"""Application bootstrap.

Why this file exists
--------------------
This is the composition root: the one module allowed to know about every other
top-level concern at once. It wires the application together and does nothing
else — no routes, no models, no business logic, no side effects beyond building
the object.

Keeping it this thin has a concrete payoff: the application can be constructed
with different settings inside a test without a process restart, and reading
this file tells a new developer the complete boot sequence in under a minute.

Bootstrap order
---------------
1. Create the ``FastAPI`` instance with metadata and the lifespan handler.
2. Register middleware (order matters — see :mod:`app.core.middleware`).
3. Register exception handlers, so every error shares one envelope.
4. Include the root API router, which owns every feature route.
"""

from fastapi import FastAPI

from app.api import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.middleware import register_middleware


def create_app() -> FastAPI:
    """Build and configure the ASGI application.

    Exposed as a factory rather than only a module-level singleton so tests can
    build an isolated instance after overriding settings.

    Returns:
        The fully configured application.
    """
    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description=settings.app.description,
        debug=settings.app.debug,
        docs_url=settings.app.docs_url,
        redoc_url=None,
        openapi_url=settings.app.openapi_url,
        lifespan=lifespan,
    )

    register_middleware(app)
    register_exception_handlers(app)
    app.include_router(api_router)

    return app


#: The ASGI callable served by uvicorn (``app.main:app``).
app = create_app()

# TODO: customise the OpenAPI schema (security schemes, servers, tags metadata)
# via a `custom_openapi` function assigned to `app.openapi`.
