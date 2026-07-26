"""The single configured ``Settings`` instance for the process.

Why this file exists
--------------------
:mod:`app.core.settings` declares *what* configuration looks like; this module
decides *when* it is built. Splitting the two matters for three reasons:

1. **One source of truth.** Every module imports the same object, so there is
   no ambiguity about which values are live.
2. **Fail fast.** Importing this module reads the environment and validates it.
   A missing ``DATABASE__URL`` crashes at import time with a precise Pydantic
   error rather than surfacing as an obscure ``None`` later.
3. **Testability.** Tests import :class:`~app.core.settings.Settings` directly
   and construct throwaway instances; nothing forces them through this module.

Usage::

    from app.core.config import settings

    engine = create_async_engine(str(settings.database.url))

Never call ``Settings()`` anywhere else in the application.
"""

from app.core.settings import Settings

#: Process-wide configuration. Built once at import time and frozen thereafter.
settings = Settings()

__all__ = ["settings"]

# TODO: expose a `get_settings()` FastAPI dependency returning `settings` so
# individual routers can be overridden with `app.dependency_overrides` in tests.
