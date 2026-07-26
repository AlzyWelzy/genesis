"""The single configured ``Settings`` instance for the process.

Why this file exists
--------------------
:mod:`app.core.settings` declares *what* configuration looks like; this module
decides *when* it is built. Splitting the two matters:

1. **One source of truth.** Every module imports the same object, so there is
   no ambiguity about which values are live.
2. **Fail fast.** Importing this module reads the environment and validates it.
   A missing ``DATABASE__URL`` crashes at import with a precise Pydantic error
   rather than surfacing as an obscure ``None`` later.
3. **Testability.** Tests construct throwaway ``Settings`` instances directly,
   or override the :func:`get_settings` dependency.

Usage::

    from app.core.config import settings

    engine = create_async_engine(str(settings.database.url))

Never call ``Settings()`` anywhere else in the application.
"""

from app.core.settings import Settings

#: Process-wide configuration. Built once at import time and frozen thereafter.
settings = Settings()


def get_settings() -> Settings:
    """FastAPI dependency returning the active settings.

    Prefer importing ``settings`` directly in application code — it is a
    frozen singleton and the indirection buys nothing. This exists for the one
    case where the indirection does pay: a test overriding configuration for a
    single endpoint via ``app.dependency_overrides[get_settings]``.
    """
    return settings


__all__ = ["get_settings", "settings"]
