"""OpenAPI schema customisation.

Why this file exists
--------------------
The generated schema is not documentation — it is a *contract artefact*. Client
SDKs are generated from it, integration tests assert against it, and partners
read it instead of asking. Left at its defaults, FastAPI produces operation IDs
like ``list_invoices_api_v1_invoices_get``, which become method names in every
generated client and change whenever a route is moved.

This module fixes three things that are expensive to fix later:

1. **Stable operation IDs.** Derived from the route's tag and function name, so
   a generated client gets ``invoices.list()`` and keeps that name when the URL
   changes.
2. **Declared security schemes.** So "Authorize" works in the docs UI and
   generated clients know how to send credentials.
3. **Documented error responses.** Clients handle the error envelope only if
   the schema says it exists — otherwise they assume 2xx is the only shape.

See ``docs/architecture/api-guidelines.md``.
"""

from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from app.common.responses import ErrorResponse
from app.core.config import settings

#: Tag metadata. Descriptions appear as section headers in the docs UI, and the
#: declared order controls the order sections are rendered — otherwise tags
#: appear in whatever order routers happened to be included.
OPENAPI_TAGS: Final[list[dict[str, Any]]] = [
    {
        "name": "system",
        "description": "Health, readiness and key distribution. Unauthenticated.",
    },
    # TODO: add one entry per feature module as it is built — a "name" matching
    # the router's tag and a "description" that says what the section covers.
]

#: Error responses attached to every operation. Documenting them once here
#: keeps a `responses={...}` block off several hundred endpoint decorators.
COMMON_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Authentication required."},
    403: {"model": ErrorResponse, "description": "Permission denied."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    422: {"model": ErrorResponse, "description": "Validation failed."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": ErrorResponse, "description": "Unexpected server error."},
}


def generate_operation_id(route: APIRoute) -> str:
    """Build a stable, readable operation ID for a route.

    Uses ``<first-tag>_<function-name>``, giving ``invoices_list`` rather than
    the default ``list_invoices_api_v1_invoices_get``. The value is what
    becomes a method name in every generated client, so it must depend on the
    handler's identity rather than on its URL — moving a route between versions
    should not rename a client method.

    Args:
        route: The route being documented.

    Returns:
        The operation ID.
    """
    if route.tags:
        return f"{route.tags[0]}_{route.name}"
    return route.name


def configure_openapi(app: FastAPI) -> None:
    """Install a customised OpenAPI schema generator on the application.

    The generated schema is cached on the app after first access, so this adds
    no per-request cost.

    Args:
        app: The application to configure.
    """

    def custom_openapi() -> dict[str, Any]:
        """Build (and memoise) the enriched OpenAPI document."""
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=settings.app.name,
            version=settings.app.version,
            description=settings.app.description,
            routes=app.routes,
            tags=OPENAPI_TAGS,
        )

        # Declared once, referenced per operation by the auth dependency. This
        # is what makes the "Authorize" button work in the docs UI.
        schema.setdefault("components", {})["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Access token issued by the authentication endpoints. "
                    "Send as: `Authorization: Bearer <token>`."
                ),
            }
        }

        schema["info"]["x-environment"] = settings.app.environment

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # ty: ignore[invalid-assignment]

    # TODO: add `servers` (staging and production base URLs) once deployed, so
    # generated clients and the docs "Try it out" button target the right host.
    # TODO: add response examples per endpoint — a schema shows the shape, an
    # example shows what a real payload looks like, and integrators read the
    # example first.
