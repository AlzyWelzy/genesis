"""Root API router — the single aggregation point for every feature route.

Why this file exists
--------------------
With hundreds of endpoints, ``main.py`` must not grow a hundred
``include_router`` calls mixed in with bootstrap code. This module separates
*what the API exposes* from *how the application boots*, which means:

* one file answers "what does this service serve?";
* the global prefix is applied in exactly one place;
* route collisions and prefix mistakes surface in one reviewable diff.

How to add a feature
--------------------
Each module in :mod:`app.modules` owns a ``router.py`` exporting an
``APIRouter`` that already declares its own ``prefix`` and ``tags``. Include it
here and nowhere else::

    from app.modules.billing.router import router as billing_router

    api_router.include_router(billing_router)

Never set a feature's prefix or tags at this level — a module must be readable
in isolation, without cross-referencing this file to learn its own URL.

Versioning
----------
When ``/v2`` arrives, create a sibling ``APIRouter(prefix="/v2")`` here and
mount both. Versioning is a routing concern; it must not leak into services.
"""

from fastapi import APIRouter

from app.core.config import settings

#: Mounted by :func:`app.main.create_app`. Every feature route hangs off this.
api_router = APIRouter(prefix=settings.app.api_prefix)

# Feature routers are included below. Intentionally empty: no module exists yet.
# TODO: include each app.modules.<feature>.router as features are implemented.
# TODO: add a /health router (liveness + readiness) before the first deploy;
# readiness must check the database and Redis, liveness must check nothing.
