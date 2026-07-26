"""Feature module discovery.

Lives in ``core`` rather than ``app/modules/`` on purpose: this is the
mechanism that *finds* features, not a feature itself. Placing it under
``app/modules/`` made every consumer — the API router, the Alembic environment,
the lifespan, the worker — appear to violate the dependency rule, when what they
actually depend on is core wiring.

It imports feature packages dynamically at runtime, which is the one sanctioned
way for an inward layer to reach outward: nothing is imported at module scope,
so the static dependency graph still points strictly inward.

Why this file exists
--------------------
Four separate places need to know which feature modules exist:

* :mod:`app.api` — to mount their routers.
* ``migrations/env.py`` — to register their models, or autogenerate silently
  produces an empty migration and the table is never created.
* :mod:`app.core.lifespan` — to import their event handlers, because a
  subscription decorator only runs when its module is imported.
* ``scripts/worker.py`` — to import their task handlers, for the same reason.

Maintaining four hand-written import lists guarantees they drift. The failure
modes are not symmetrical, and three of the four are *silent*: a missing router
import gives an obvious 404, but a missing model import produces a migration
that looks fine and creates nothing, and a missing handler import produces an
event system that appears to work while nothing listens.

This module replaces all four lists with discovery. A feature is registered by
existing on disk — there is nothing to remember and nothing to keep in sync.

How discovery works
-------------------
Every direct subpackage of :mod:`app.modules` is a feature. For each one, the
conventional submodules are imported if present and skipped if absent:

===============  ==========================================================
``router``       must export ``router: APIRouter``
``models``       imported for its side effect of registering on ``Base``
``handlers``     imported for its side effect of registering subscriptions
``tasks``        imported for its side effect of registering task handlers
===============  ==========================================================

An import error inside a feature is **never** swallowed. A typo in a model
module would otherwise present as a mysteriously missing table.

Why not a plugin system
-----------------------
This is deliberately convention over configuration, but it is *narrow*
convention: fixed submodule names, one fixed location, no entry points, no
manifest files. The whole mechanism is one file that a reader can hold in their
head, which is the property that makes implicit behaviour acceptable.
"""

import importlib
import pkgutil
from types import ModuleType
from typing import Final

from fastapi import APIRouter

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Package containing the feature modules.
MODULES_PACKAGE: Final[str] = "app.modules"

#: Submodules imported for their side effects — model registration, event
#: subscriptions, task registration. Order matters: models first, so anything
#: that references them at import time already has them.
SIDE_EFFECT_SUBMODULES: Final[tuple[str, ...]] = ("models", "handlers", "tasks")

#: Submodule expected to export an ``APIRouter`` named ``router``.
ROUTER_SUBMODULE: Final[str] = "router"


def discover_feature_names() -> tuple[str, ...]:
    """Return the name of every feature package under :mod:`app.modules`.

    Returns:
        Feature names in a stable, sorted order. Sorted rather than
        filesystem order so route registration, and therefore the OpenAPI
        document, is byte-for-byte reproducible across machines.
    """
    package = importlib.import_module(MODULES_PACKAGE)
    return tuple(
        sorted(
            info.name
            for info in pkgutil.iter_modules(package.__path__)
            if info.ispkg and not info.name.startswith("_")
        )
    )


def import_feature_submodule(feature: str, submodule: str) -> ModuleType | None:
    """Import ``app.modules.<feature>.<submodule>`` if it exists.

    Args:
        feature: Feature package name.
        submodule: Submodule to import.

    Returns:
        The imported module, or ``None`` when the feature does not define it.

    Raises:
        ImportError: When the submodule exists but fails to import. Never
            swallowed: an ``ImportError`` raised *by* a model module looks
            identical to that module not existing, and the consequence — a
            table that is silently never created — is far worse than a crash.
    """
    name = f"{MODULES_PACKAGE}.{feature}.{submodule}"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        # Only treat it as "not defined" when *this* module is the one missing.
        # A ModuleNotFoundError for one of its own imports must propagate.
        if exc.name == name:
            return None
        raise


def import_side_effect_modules() -> dict[str, tuple[str, ...]]:
    """Import every feature's models, handlers and tasks.

    Called from the Alembic environment, the application lifespan and the
    worker entry point. Idempotent — Python caches modules, so repeated calls
    are free and the registration decorators run exactly once.

    Returns:
        Mapping of feature name to the submodules that were actually imported,
        for logging and for a startup assertion.
    """
    imported: dict[str, tuple[str, ...]] = {}
    for feature in discover_feature_names():
        found = tuple(
            submodule
            for submodule in SIDE_EFFECT_SUBMODULES
            if import_feature_submodule(feature, submodule) is not None
        )
        imported[feature] = found
        logger.debug("Loaded feature %s: %s", feature, ", ".join(found) or "none")
    return imported


def collect_routers() -> list[tuple[str, APIRouter]]:
    """Import every feature's router module and return the routers.

    Returns:
        ``(feature_name, router)`` pairs in sorted feature order.

    Raises:
        TypeError: When a ``router`` module exists but does not export an
            ``APIRouter`` named ``router``. Failing loudly at startup beats a
            feature whose endpoints are quietly absent in production.
    """
    routers: list[tuple[str, APIRouter]] = []
    for feature in discover_feature_names():
        module = import_feature_submodule(feature, ROUTER_SUBMODULE)
        if module is None:
            continue

        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            raise TypeError(
                f"{MODULES_PACKAGE}.{feature}.{ROUTER_SUBMODULE} must export an "
                "APIRouter named `router`."
            )
        routers.append((feature, router))
    return routers


def collect_router_tags() -> list[str]:
    """Return every tag declared by a feature router.

    Used to build the OpenAPI tag metadata without a second hand-written list
    that would drift from the routers themselves.
    """
    tags: list[str] = []
    for _, router in collect_routers():
        for route in router.routes:
            for tag in getattr(route, "tags", []):
                if tag not in tags:
                    tags.append(str(tag))
    return tags
