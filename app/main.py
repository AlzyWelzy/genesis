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
2. Register middleware — order is semantic, see :mod:`app.core.middleware`.
3. Register exception handlers, so every error shares one envelope.
4. Mount the operational routes at the root, outside the API prefix.
5. Mount the versioned API router, which owns every feature route.
6. Customise the OpenAPI schema.
"""

from fastapi import FastAPI

from app.api import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.middleware import register_middleware
from app.core.openapi import (
    COMMON_ERROR_RESPONSES,
    configure_openapi,
    generate_operation_id,
)
from app.system.router import router as system_router


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
        root_path=settings.app.root_path,
        lifespan=lifespan,
        # Stable operation IDs: these become method names in generated clients,
        # so they must not change when a route moves.
        generate_unique_id_function=generate_operation_id,
        # Documented once globally rather than on several hundred decorators.
        responses=COMMON_ERROR_RESPONSES,
    )

    register_middleware(app)
    register_exception_handlers(app)

    # Operational routes at the root: orchestrators expect /live and /ready
    # there, and a probe that 404s because the API prefix changed gets every
    # replica killed.
    app.include_router(system_router)
    app.include_router(api_router)

    configure_openapi(app)

    return app


#: The ASGI callable served by uvicorn (``app.main:app``).
app = create_app()
