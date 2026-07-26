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

from app.common.responses import error_responses
from app.core.config import settings
from app.core.discovery import collect_router_tags

#: Tag metadata. Descriptions appear as section headers in the docs UI, and the
#: declared order controls the order sections are rendered — otherwise tags
#: appear in whatever order routers happened to be included.
SYSTEM_TAG: Final[dict[str, Any]] = {
    "name": "system",
    "description": "Health, readiness and key distribution. Unauthenticated.",
}

#: Human-written descriptions for feature tags. A tag with no entry here still
#: appears — discovered from the routers — just without prose. Optional by
#: design: a missing description is a documentation gap, not a broken build.
FEATURE_TAG_DESCRIPTIONS: Final[dict[str, str]] = {}


def build_openapi_tags() -> list[dict[str, Any]]:
    """Assemble tag metadata from the system tag plus discovered feature tags.

    Derived from the routers rather than hand-listed, so the docs cannot drift
    from what is actually mounted. Declared order controls render order;
    otherwise sections appear in whatever order routers happened to load.
    """
    tags: list[dict[str, Any]] = [SYSTEM_TAG]
    for tag in collect_router_tags():
        if tag == SYSTEM_TAG["name"]:
            continue
        entry: dict[str, Any] = {"name": tag}
        if description := FEATURE_TAG_DESCRIPTIONS.get(tag):
            entry["description"] = description
        tags.append(entry)
    return tags


#: Error responses attached to every operation. Documenting them once here
#: keeps a `responses={...}` block off several hundred endpoint decorators.
COMMON_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = error_responses(
    401, 403, 404, 422, 429, 500
)


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
            tags=build_openapi_tags(),
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

        # Declared so generated clients and the docs "Try it out" button target
        # the right host. Without it they assume the host the schema was fetched
        # from, which is wrong for a schema committed to a client repository.
        if settings.app.api_base_url:
            schema["servers"] = [
                {
                    "url": settings.app.api_base_url,
                    "description": settings.app.environment,
                }
            ]

        # A schema shows the *shape* of an error; an example shows what one
        # actually looks like. Integrators read the example first, and a wrong
        # guess about the envelope is the most common integration bug.
        components = schema.setdefault("components", {})
        components.setdefault("examples", {})["ErrorEnvelope"] = {
            "summary": "Standard error envelope",
            "value": {
                "error": {
                    "code": "not_found",
                    "message": "Resource not found.",
                    "request_id": "0193f4a27c1170008f3a1b2c3d4e5f60",
                }
            },
        }

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # ty: ignore[invalid-assignment]
