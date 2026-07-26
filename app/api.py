"""Root API router — the single aggregation point for every feature route.

Why this file exists
--------------------
With hundreds of endpoints, ``main.py`` must not grow a hundred
``include_router`` calls mixed in with bootstrap code. This module separates
*what the API exposes* from *how the application boots*:

* one file answers "what does this service serve?";
* the global prefix and version are applied in exactly one place;
* route collisions and prefix mistakes surface in one reviewable diff.

Versioning
----------
Routes are mounted under ``/api/v1``. The version lives here, not in the
feature routers, so that introducing ``/api/v2`` is a second router in this
file rather than an edit to every module.

Versioning is a *routing* concern and must not leak into services. When v2
arrives, both versions call the same service layer with different schemas
mapping to it — if a service needs to know which version called it, the
versioning boundary has been drawn in the wrong place.

How to add a feature
--------------------
Each module in :mod:`app.modules` owns a ``router.py`` exporting an
``APIRouter`` that already declares its own ``prefix`` and ``tags``. Include it
here and nowhere else::

    from app.modules.billing.router import router as billing_router

    v1_router.include_router(billing_router)

Never set a feature's prefix or tags at this level — a module must be readable
in isolation, without cross-referencing this file to learn its own URL.
"""

from fastapi import APIRouter

from app.core.config import settings

#: Version 1 of the API. Feature routers attach here.
v1_router = APIRouter(prefix=settings.app.version_prefix)

# Feature routers are included below. Intentionally empty: no module exists yet.
# TODO: include each app.modules.<feature>.router as features are implemented.

#: Mounted by :func:`app.main.create_app`. Aggregates every API version.
api_router = APIRouter()
api_router.include_router(v1_router)
