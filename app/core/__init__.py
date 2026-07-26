"""Framework and application configuration.

Owns everything that makes this an *application* rather than a library:
settings, logging, security primitives, lifespan, middleware and the global
exception handlers.

Nothing here may import from :mod:`app.modules`. Core is the innermost layer;
an import in that direction makes the application impossible to boot without
every feature present.
"""
