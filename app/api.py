"""Root API router — the single aggregation point for every feature route.

Why this file exists
--------------------
With hundreds of endpoints, ``main.py`` must not grow a hundred
``include_router`` calls mixed in with bootstrap code. This module separates
*what the API exposes* from *how the application boots*:

* one file answers "what does this service serve?";
* the global prefix and version are applied in exactly one place;
* route collisions and prefix mistakes surface in one reviewable diff.

Discovery, not a list
---------------------
Feature routers are found by :mod:`app.core.discovery` rather than imported
by hand. A hand-maintained list is one more thing to update when adding a
feature, and forgetting it produces a 404 that looks like a routing bug.

A feature is mounted by existing: create ``app/modules/<feature>/router.py``
exporting ``router: APIRouter``, and it appears here. The router declares its
own ``prefix`` and ``tags``, so a module stays readable in isolation without
cross-referencing this file to learn its own URL.

Versioning
----------
Routes mount under ``/api/v1``. The version lives here, not in the feature
routers, so introducing ``/api/v2`` is a second router in this file rather than
an edit to every module.

Versioning is a *routing* concern and must not leak into services. When v2
arrives, both versions call the same service layer through different schemas.
If a service needs to know which version called it, the boundary has been drawn
in the wrong place.
"""

from fastapi import APIRouter

from app.core.config import settings
from app.core.discovery import collect_routers
from app.core.logging import get_logger

logger = get_logger(__name__)


def build_v1_router() -> APIRouter:
    """Build the version 1 router with every discovered feature mounted."""
    router = APIRouter(prefix=settings.app.version_prefix)
    for feature, feature_router in collect_routers():
        router.include_router(feature_router)
        logger.debug("Mounted feature router: %s", feature)
    return router


#: Version 1 of the API. Feature routers attach here.
v1_router = build_v1_router()

#: Mounted by :func:`app.main.create_app`. Aggregates every API version.
api_router = APIRouter()
api_router.include_router(v1_router)
